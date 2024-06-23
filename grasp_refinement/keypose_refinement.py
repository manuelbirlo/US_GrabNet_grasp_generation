import argparse
import os
import pickle
import sys
import gc

sys.path.append('.')
sys.path.append('..')
import numpy as np
import open3d as o3d
import torch
import trimesh
from scipy.io import loadmat, savemat
from scipy.spatial.transform import Rotation as Ro
from torch.utils.data import DataLoader

import mano
from dataset.HO3D_diversity_generation import HO3D_diversity
from metric.simulate import run_simulation
from network.affordanceNet_obman_mano_vertex import affordanceNet
from network.cmapnet_objhand import pointnet_reg
from utils import utils, utils_loss
from utils.loss import TTT_loss

############## set up section ##################################################################
# get finger tip from vertices on mano hand
# TIP_IDS = {
#     'mano': {
#             'thumb':		744,
#             'index':		320,
#             'middle':		443,
#             'ring':		    554,
#             'pinky':		671,
#         }
# }

TPID = [744, 320, 443, 554, 671]

# mano right hand location
MANO_PATH = '../data/bodymodel/mano/MANO_RIGHT.pkl'
# output folder location that contains all generated outputs
OUTPUT_BASE = './refined_subsamples/'
OBJECT_LOCATION = '/home/ray/Downloads/Tools'
vhacd_exe = " optional path to vhacd executable"
inmat_this = '../grasp_generation/OUT/generate.mat'

###################################################################################################
def load_obj_verts(mesh_path, rand_rotmat, rndrotate=True, scale=1., n_sample_verts=3000):
    np.random.seed(100)
    obj_mesh = Mesh(filename=mesh_path, vscale=scale)

    ## center and scale the object
    max_length = np.linalg.norm(obj_mesh.vertices, axis=1).max()
    if max_length > 1:
        re_scale = max_length / .08
        print(f'The object is very large, down-scaling by {re_scale} factor')
        obj_mesh.vertices[:] = obj_mesh.vertices / re_scale

    object_fullpts = obj_mesh.vertices
    maximum = object_fullpts.max(0, keepdims=True)
    minimum = object_fullpts.min(0, keepdims=True)

    offset = (maximum + minimum) / 2
    verts_obj = object_fullpts - offset
    obj_mesh.vertices[:] = verts_obj

    if rndrotate:
        obj_mesh.rotate_vertices(rand_rotmat)
    else:
        rand_rotmat = np.eye(3)

    while (obj_mesh.vertices.shape[0] < n_sample_verts):
        new_mesh = obj_mesh.subdivide()
        obj_mesh = Mesh(vertices=new_mesh.vertices,
                        faces=new_mesh.faces,
                        visual=new_mesh.visual)
    verts_sampled, _ = trimesh.sample.sample_surface_even(obj_mesh, n_sample_verts, radius=None)

    return verts_sampled, obj_mesh, rand_rotmat


def load_obj_verts_diskplacer(mesh_path, mesh_handle, rand_rotmat, rndrotate=True, scale=1., n_sample_verts=3000):
    np.random.seed(100)
    obj_mesh = Mesh(filename=mesh_path, vscale=scale)
    obj_handle = Mesh(filename=mesh_handle, vscale=scale)

    # if the object has no texture, make it yellow

    ## center and scale the object
    max_length = np.linalg.norm(obj_mesh.vertices, axis=1).max()
    if max_length > 1:
        re_scale = max_length / .08
        print(f'The object is very large, down-scaling by {re_scale} factor')
        obj_mesh.vertices[:] = obj_mesh.vertices / re_scale

    object_fullpts = obj_handle.vertices
    maximum = object_fullpts.max(0, keepdims=True)
    minimum = object_fullpts.min(0, keepdims=True)

    object_fullpts = obj_mesh.vertices

    offset = (maximum + minimum) / 2
    verts_obj = object_fullpts - offset
    obj_mesh.vertices[:] = verts_obj

    if rndrotate:
        obj_mesh.rotate_vertices(rand_rotmat)
    else:
        rand_rotmat = np.eye(3)

    verts_sampled, _ = trimesh.sample.sample_surface_even(obj_mesh, n_sample_verts, radius=None)

    return verts_sampled, obj_mesh, rand_rotmat


class Mesh(trimesh.Trimesh):

    def __init__(self,
                 filename=None,
                 vertices=None,
                 faces=None,
                 vc=None,
                 fc=None,
                 vscale=None,
                 radius=.002,
                 process=False,
                 visual=None,
                 wireframe=False,
                 smooth=False,
                 **kwargs):

        self.wireframe = wireframe
        self.smooth = smooth

        if filename is not None:
            mesh = trimesh.load(filename, process=process)
            vertices = mesh.vertices
            faces = mesh.faces
            visual = mesh.visual
        if vscale is not None:
            vertices = vertices * vscale

        super(Mesh, self).__init__(vertices=vertices, faces=faces, process=process, visual=visual)

        if vc is not None:
            self.set_vertex_colors(vc)
        if fc is not None:
            self.set_face_colors(fc)

    def rotate_vertices(self, rxyz):
        visual = self.visual
        self.vertices[:] = np.array(self.vertices @ rxyz.T)
        self.visual = visual
        return self

    def colors_like(self, color, array, ids):

        color = np.array(color)

        if color.max() <= 1.:
            color = color * 255
        color = color.astype(np.int8)

        n_color = color.shape[0]
        n_ids = ids.shape[0]

        new_color = np.array(array)
        if n_color <= 4:
            new_color[ids, :n_color] = np.repeat(color[np.newaxis], n_ids, axis=0)
        else:
            new_color[ids, :] = color

        return new_color

    def set_vertex_colors(self, vc, vertex_ids=None):

        all_ids = np.arange(self.vertices.shape[0])
        if vertex_ids is None:
            vertex_ids = all_ids

        vertex_ids = all_ids[vertex_ids]
        new_vc = self.colors_like(vc, self.visual.vertex_colors, vertex_ids)
        self.visual.vertex_colors[:] = new_vc

    def set_face_colors(self, fc, face_ids=None):

        if face_ids is None:
            face_ids = np.arange(self.faces.shape[0])

        new_fc = self.colors_like(fc, self.visual.face_colors, face_ids)
        self.visual.face_colors[:] = new_fc

    @staticmethod
    def concatenate_meshes(meshes):
        return trimesh.util.concatenate(meshes)


def intersect_vox_original(obj_mesh, hand_mesh, pitch=0.5):
    '''
    Evaluating intersection between hand and object
    :param pitch: voxel size
    :return: intersection volume
    '''
    # obj_vox = obj_mesh.voxelized(pitch=pitch)
    # obj_points = obj_vox.points
    # inside = hand_mesh.contains(obj_points)
    # volume = inside.sum() * np.power(pitch, 3)
    hand_vox = hand_mesh.voxelized(pitch=pitch)
    hand_points = hand_vox.points
    inside = obj_mesh.contains(hand_points)
    volume = inside.sum() / len(hand_points)  # * np.power(pitch, 3)
    return volume

def intersect_vox(obj_mesh, hand_mesh, pitch=0.5, batch_size=10000):
    try:
        pitch = pitch * 2  # Increase pitch
        
        # Simplify the hand mesh (reduce vertices)
        hand_mesh = hand_mesh.simplify_quadric_decimation(
           int(hand_mesh.vertices.shape[0] * 0.5)
        )
        
        # Voxelize the hand mesh
        hand_vox = hand_mesh.voxelized(pitch=pitch)
        hand_points = hand_vox.points
        
        # Initialize volume
        volume = 0
        
        # Process in batches
        for i in range(0, len(hand_points), batch_size):
            batch_points = hand_points[i:i + batch_size]
            inside = obj_mesh.contains(batch_points)
            volume += inside.sum()  # Update volume
        
        volume /= len(hand_points)  # Normalize volume

        return volume
    finally:
        gc.collect()


#def mesh_vert_int_exts(obj1_mesh, obj2_verts):
#    inside = obj1_mesh.ray.contains_points(obj2_verts)
#    sign = (inside.astype(int) * 2) - 1
#    return sign

def mesh_vert_int_exts(obj1_mesh, obj2_verts, batch_size=10000):
    """Applys batch processing to reduce resource consumption"""
    n_verts = obj2_verts.shape[0]
    signs = []

    for i in range(0, n_verts, batch_size):
        batch_verts = obj2_verts[i:i + batch_size]
        inside = obj1_mesh.ray.contains_points(batch_verts)
        sign_batch = (inside.astype(int) * 2) - 1
        signs.append(sign_batch)
        
        gc.collect()  # Collect garbage after processing each batch

    # Concatenate results from all batches
    signs = np.concatenate(signs)

    return signs

def show_pcd(list_1):
    a = []
    import open3d as o3d
    for i in list_1:
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(i)
        a.append(pcd)

    o3d.visualization.draw_geometries(a)


FRIEM_SELECTION = [321, 326, 331, 342, 347, 365, 381, 384, 392, 393, 397, 400, 464, 468, 478, 492, 493]
DISKPLACER_SELECTION = [1, 8, 19, 25, 27, 28, 32, 46, 82, 83, 87, 100, 105, 108, 111, 119, 138, 149, 163, 165, 168, 176,
                        177, 187, 192, 204, 205, 240, 248,
                        250, 259, 261, 265, 276, 290, 293, 309, 313, 321, 341, 348, 359, 362, 364, 366, 390, 400, 402,
                        433, 439,
                        432, 454, 458, 475, 480, 499]

SCALEPL_SELECTION = [1, 7, 16, 41, 58, 81, 83, 90, 98, 110, 157, 169, 170, 176, 194, 201, 215, 216, 250, 252, 257, 259,
                     269,
                     341, 387, 393, 422, 436, 474, 485, 491, 496]

def main(args, model, cmap_model, device, rh_mano, rh_faces, inmat=None,using_contactnet=False):
    """
    Generate diverse grasps for object index with args.obj_id in out-of-domain HO3D object models
    """

    if using_contactnet:
        model.eval()
        cmap_model.eval()
    rh_mano.eval()
    if True:

        ############################

        OUT_dir = os.path.join(OUTPUT_BASE, inmat.split('/')[-3], inmat.split('/')[-2])
        print("_____________________ OUT_dir {}".format(OUT_dir))
        os.makedirs(OUT_dir, exist_ok=True)
        all_valid = []
        all_generated = loadmat(inmat)
        all_order_list = []
        print(inmat)
        print("________________________ {}".format(all_generated['rotmat']))
        print("----------------------shape ------- {}".format(all_generated['rotmat'].shape[0]))
        #for i in (len(all_generated['rotmat'])): #range(11,23):
        for i in range(0, all_generated['rotmat'].shape[0] - 1):
            index_temp = i
            print("____ INDEX ___ {}".format(index_temp))
            if 'friem' in inmat:
                verts_obj, mesh_obj, rotmat = load_obj_verts('../data/TOOLS_Release/Friem_original.ply',
                                                             all_generated['rotmat'][index_temp] @ Ro.from_euler('z', 0,
                                                                                                                 degrees=True).as_matrix(),
                                                             rndrotate=True,
                                                             scale=0.001)
            elif 'diskplacer' in inmat:
                verts_obj, mesh_obj, rotmat = load_obj_verts_diskplacer('../data/TOOLS_Release/DiskPlacer.stl',
                                                                        '../data/TOOLS_Release/DiskPlacer_handle.ply',
                                                                        all_generated['rotmat'][
                                                                            index_temp] @ Ro.from_euler('z', 0,
                                                                                                        degrees=True).as_matrix(),
                                                                        rndrotate=True,
                                                                        scale=0.001)
            elif 'scalpel' in inmat:
                verts_obj, mesh_obj, rotmat = load_obj_verts('../data/TOOLS_Release/Scalpel.stl',
                                                             all_generated['rotmat'][index_temp] @ Ro.from_euler('z', 0,
                                                                                                                 degrees=True).as_matrix(),
                                                             rndrotate=True,
                                                             scale=0.001)
            if 'generate' in inmat:
                verts_obj, mesh_obj, rotmat = load_obj_verts('../assets/ClariusC3_2c_painted_1_downscaled.ply',
                                                             all_generated['rotmat'][index_temp] @ Ro.from_euler('z', 0,
                                                                                                    degrees=True).as_matrix(),
                                                             rndrotate=True,
                                                             scale=0.001)
            else:
                print('WRONG')
            # verts_obj, mesh_obj, rotmat = load_obj_verts('/home/rui/Downloads/Tools/used_tool/Friem_original.ply',
            #                                              all_generated['rotmat'][index_temp] @ Ro.from_euler('z', 0, degrees=True).as_matrix(), rndrotate=True,
            #                                              scale=0.001)

            # Original code that causes error if .ply file has inconsistent number of vertices that are != 3000
            obj_pc_TTT = np.concatenate((verts_obj, np.ones((3000, 1)) * 0.2248), 1)
            obj_pc_TTT = torch.from_numpy(obj_pc_TTT).permute(1, 0).view(1, 4, 3000).float().to(device)

            """   
            # ---  Modified code to handle dynamic number of vertices ----
            num_verts = verts_obj.shape[0]  # Actual number of vertices
            # obj_pc_TTT is the concatenated array from before with the actual number of vertices
            obj_pc_TTT = np.concatenate((verts_obj, np.ones((num_verts, 1)) * 0.2248), axis=1)

            # Convert numpy array to PyTorch tensor
            obj_pc_TTT = torch.from_numpy(obj_pc_TTT).float()

            # Pad the tensor to have 3000 points if necessary
            if num_verts < 3000:
                padding = torch.zeros((3000 - num_verts, 4))  # Assuming the padding value for the last column should be zero
                padding[:, -1] = 0.2248  # Set the last column to the fixed value if needed
                obj_pc_TTT = torch.cat([obj_pc_TTT, padding], dim=0)

            # Make sure it's permuted and unsqueezed to match the expected input shape for the network
            obj_pc_TTT = obj_pc_TTT.permute(1, 0).unsqueeze(0)  # The shape is now (1, 4, 3000)

            # Send to the appropriate device
            obj_pc_TTT = obj_pc_TTT.to(device)
            """

            this_global_orient = all_generated['global_orient'][[index_temp], :]
            this_joints = all_generated['joints'][[index_temp], :]
            this_joints = torch.from_numpy(this_joints).float().to(device)
            this_hand_pose = all_generated['hand_pose'][[index_temp], :]
            this_transl = all_generated['transl'][[index_temp], :]
            
            print("___________________________ {}".format(inmat[:-12] + "test_meshes/" + str(index_temp).zfill(6) + '_Hand.ply'))
            #temp_hand = Mesh(filename=inmat[:-12] + str(index_temp).zfill(6) + '_Hand.ply')
            temp_hand = Mesh(filename=inmat[:-12] + "test_meshes/" + str(index_temp).zfill(6) + '_Hand.ply')
        

            temp_hand_v = temp_hand.vertices
            temp_hand_v = temp_hand_v @ rotmat.T
            this_vert = torch.from_numpy(temp_hand_v).float().to(device)

            this_hand_beta = np.array([[0, 0, 0, 0, 0, 0, 0, 0, 0, 0]])
            recon_param = torch.from_numpy(this_hand_pose).view(1, 45).float().to(device)

            recon_param = torch.autograd.Variable(recon_param, requires_grad=True)
            optimizer = torch.optim.SGD([recon_param], lr=1e-6, momentum=0.8)

            l2loss = torch.nn.MSELoss()

            #for j in range(1501):  # non-learning based optimization steps
            for j in range(1501):
                optimizer.zero_grad()

                recon_mano = rh_mano(betas=torch.from_numpy(this_hand_beta).float().to(device),
                                     global_orient=torch.from_numpy(this_global_orient).float().to(device),
                                     hand_pose=recon_param, transl=torch.from_numpy(this_transl).float().to(device))

                recon_xyz = recon_mano.vertices.float().to(device)  # [B,778,3], hand vertices
                recon_joints = recon_mano.joints.to(device)

                obj_nn_dist_affordance, _ = utils_loss.get_NN(obj_pc_TTT.permute(0, 2, 1)[:, :, :3], recon_xyz)
                cmap_affordance = utils.get_pseudo_cmap(obj_nn_dist_affordance)  # [B,3000]

                # predict target cmap by ContactNet

                if using_contactnet:
                    recon_cmap = cmap_model(obj_pc_TTT[:, :3, :], recon_xyz.permute(0, 2, 1).contiguous())  # [B,3000]
                    recon_cmap = (recon_cmap / torch.max(recon_cmap, dim=1)[0]).detach()
                else:
                    recon_cmap = torch.zeros([1,3000]).to(device)

                penetr_loss, consistency_loss, contact_loss, finger_contact_loss = TTT_loss(recon_xyz, rh_faces,
                                                                                            obj_pc_TTT[:, :3,
                                                                                            :].permute(0, 2,
                                                                                                       1).contiguous(),
                                                                                            cmap_affordance, recon_cmap)
                kp_weight = 10

                new_joint = torch.cat((recon_joints, recon_xyz[:, TPID, :]), 1)
                gt_joint = torch.cat((this_joints, this_vert[TPID, :].unsqueeze(0)), 1)
                kp_loss = l2loss(new_joint * 100, gt_joint * 100)

                if 'diskplacer' in inmat:
                    PENE_TRA = 0.03
                    loss = 60 * contact_loss + 0 * consistency_loss + 300 * penetr_loss + kp_weight * kp_loss
                elif 'friem' in inmat:
                    PENE_TRA = 0.01
                    loss = 20 * contact_loss + 0 * consistency_loss + 300 * penetr_loss + kp_weight * kp_loss
                elif 'generate' in inmat:
                    PENE_TRA = 0.02
                    #loss = 20 * contact_loss + 0 * consistency_loss + 300 * penetr_loss + kp_weight * kp_loss
                    loss = 20 * contact_loss + 0 * consistency_loss + 300 * penetr_loss + kp_weight * kp_loss
                else:
                    PENE_TRA = 0.01
                    loss = 100 * contact_loss + 0 * consistency_loss + 30 * penetr_loss + kp_weight * kp_loss

                loss.backward()
                optimizer.step()
                if j % 300 == 0:
                    print("iter {}, "
                          "penetration loss {:9.5f}, "
                          "kp_loss loss {:9.5f}, "
                          "contact loss {:9.5f}".format(  j,
                                                        penetr_loss.item(), kp_loss.item(), contact_loss.item()))
                gc.collect()

            # evaluate grasp

            obj_mesh = mesh_obj

            print("_______rh_mano_________ ")
            final_mano = rh_mano(betas=torch.from_numpy(this_hand_beta).float().to(device),
                                 global_orient=torch.from_numpy(this_global_orient).float().to(device),
                                 hand_pose=recon_param, transl=torch.from_numpy(this_transl).float().to(device))
            save_hand_dict = {}
            save_hand_dict['betas'] = this_hand_beta
            save_hand_dict['global_orient'] = this_global_orient
            save_hand_dict['hand_pose'] = recon_param.detach().cpu().numpy()
            save_hand_dict['transl'] = this_transl

            final_mano_verts = final_mano.vertices.squeeze(0).detach().cpu().numpy()  # [778, 3]
            all_order_list.append(final_mano.joints[0, 3, :].detach().cpu().numpy())

            print("___________ trimesh_Trimesh_______")
            hand_mesh = trimesh.Trimesh(vertices=final_mano_verts, faces=rh_faces.squeeze(0).cpu().numpy())

            mesh_1 = o3d.geometry.TriangleMesh()
            mesh_1.vertices = o3d.utility.Vector3dVector(final_mano_verts)
            mesh_1.triangles = o3d.utility.Vector3iVector(rh_faces.squeeze(0).detach().cpu().numpy())

            mesh_2 = o3d.geometry.TriangleMesh()
            mesh_2.vertices = o3d.utility.Vector3dVector(obj_mesh.vertices)
            mesh_2.triangles = o3d.utility.Vector3iVector(obj_mesh.faces)

            print("_____________ intersect_vox_______________")
            penetr_vol = intersect_vox(obj_mesh, hand_mesh, pitch=0.005)
            print(penetr_vol)
            # contact
            penetration_tol = 0.005

            print("____________________ closest point______________")
            #result_close, result_distance, _ = trimesh.proximity.closest_point(obj_mesh, final_mano_verts)

            # ---------------- batch processing to reduce memory usage -------------------------
            # Assuming final_mano_verts is a numpy array of shape (n_verts, 3)
            # and obj_mesh is a trimesh object

            batch_size = 10 #1000  # Adjust batch size according to your memory constraints
            n_verts = final_mano_verts.shape[0]

            # Initialize an empty list to store the results
            all_distances = []

            for i in range(0, n_verts, batch_size):
                # Create a batch of vertices
                batch_verts = final_mano_verts[i:i + batch_size]
                
                # Process the batch
                _, result_distance, _ = trimesh.proximity.closest_point(obj_mesh, batch_verts)
                
                # Store the results
                all_distances.append(result_distance)
                gc.collect()

            # Combine the results from all batches
            all_distances = np.hstack(all_distances)

            # ----------------------------------------------------------------------------------------
            print("____________________  mesh_vert_int_exts___")
            sign = mesh_vert_int_exts(obj_mesh, final_mano_verts, batch_size=100)
            #nonzero = result_distance > penetration_tol
            nonzero = all_distances > penetration_tol
            exterior = [sign == -1][0] & nonzero
            contact = ~exterior
            sample_contact = contact.sum() > 0
            print("sample_contact: {}".format(sample_contact))
            # simulation displacement

            try:
                print("______running simulation ....")
                simu_disp = run_simulation(final_mano_verts, rh_faces.reshape((-1, 3)),
                                           obj_mesh.vertices, obj_mesh.faces,
                                           vhacd_exe=vhacd_exe, sample_idx=i)
                print("..... completed simulation")
            except:
                simu_disp = 0.00010
                print('NO SIMULATE DISPLACEMENT PERFORMED!')
                #### pass anyway

            #### 0.01 for friem
            save_flag = (penetr_vol < PENE_TRA) and (simu_disp < args.simu_disp_thre) and sample_contact
            print('generate id: {}, penetr vol: {}, simu disp: {}, contact: {}, save flag: {}'
                  .format(i, penetr_vol, simu_disp, sample_contact, save_flag))
            if save_flag:
                all_valid.append(index_temp)

                o3d.io.write_triangle_mesh(os.path.join(OUT_dir, str(index_temp).zfill(5) + '_Hand.ply'), mesh_1)
                o3d.io.write_triangle_mesh(os.path.join(OUT_dir, str(index_temp).zfill(5) + '_Object.ply'), mesh_2)
                output_path_pkl_addtion = os.path.join(OUT_dir, str(index_temp).zfill(5) + '_MANO.pkl')

                with open(output_path_pkl_addtion, 'wb') as f:
                    pickle.dump(save_hand_dict, f)

        temp_list = [temp[2] for temp in all_order_list]
        savemat(os.path.join(OUT_dir, 'valid.mat'), {'all_valid': all_valid})
        # print(np.array(np.argsort(temp_list)))
        file = open(os.path.join(OUT_dir, "order.txt"), "w+")

        # Saving the 2D array in a text file
        content = str(np.array(np.argsort(temp_list)))
        file.write(content)
        file.close()



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    '''experiment setting'''
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--batch_size", type=int, default=1)
    parser.add_argument("--use_cuda", type=int, default=1)
    parser.add_argument("--dataloader_workers", type=int, default=32)
    '''affordance network information'''
    parser.add_argument("--affordance_model_path", type=str, default='checkpoints/model_affordance_best_full.pth')
    parser.add_argument("--encoder_layer_sizes", type=list, default=[1024, 512, 256])
    parser.add_argument("--decoder_layer_sizes", type=list, default=[1024, 256, 61])
    parser.add_argument("--latent_size", type=int, default=64)
    parser.add_argument("--obj_inchannel", type=int, default=4)
    parser.add_argument("--condition_size", type=int, default=1024)
    '''cmap network information'''
    parser.add_argument("--cmap_model_path", type=str, default='checkpoints/model_cmap_best.pth')
    '''Generated graps information'''
    parser.add_argument("--obj_id", type=int, default=6)
    # You can change the two thresholds to save the graps you want
    parser.add_argument("--penetr_vol_thre", type=float, default=9e-6)  # 4cm^3
    parser.add_argument("--simu_disp_thre", type=float, default=0.03)  # 3cm
    parser.add_argument("--num_grasp", type=int, default=100)  # number of grasps you want to generate
    args = parser.parse_args()
    assert args.obj_id in [3, 4, 6, 10, 11, 19, 21, 25, 35, 37, 99]

    # device
    use_cuda = args.use_cuda and torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")
    print("using device", device)

    # network
    affordance_model = affordanceNet(obj_inchannel=args.obj_inchannel,
                                     cvae_encoder_sizes=args.encoder_layer_sizes,
                                     cvae_latent_size=args.latent_size,
                                     cvae_decoder_sizes=args.decoder_layer_sizes,
                                     cvae_condition_size=args.condition_size)  # GraspCVAE
    cmap_model = pointnet_reg(with_rgb=False)  # ContactNet
    using_contactnet = False
    # load pre-trained model
    if using_contactnet:
        checkpoint_affordance = torch.load(args.affordance_model_path, map_location=torch.device('cpu'))['network']
        affordance_model.load_state_dict(checkpoint_affordance)
        affordance_model = affordance_model.to(device)
        checkpoint_cmap = torch.load(args.cmap_model_path, map_location=torch.device('cpu'))['network']
        cmap_model.load_state_dict(checkpoint_cmap)
        cmap_model = cmap_model.to(device)
    else:
        affordance_model = None
        cmap_model = None

    # dataset
    # dataset = HO3D_diversity()
    # dataloader = DataLoader(dataset=dataset, batch_size=1, shuffle=False, num_workers=1)
    # mano hand model
    with torch.no_grad():
        rh_mano = mano.load(model_path=MANO_PATH,
                            model_type='mano',
                            num_pca_comps=45,
                            batch_size=1,
                            flat_hand_mean=True).to(device)
    rh_faces = torch.from_numpy(rh_mano.faces.astype(np.int32)).view(1, -1, 3).to(device)  # [1, 1538, 3], face indexes

    main(args, affordance_model, cmap_model, device, rh_mano, rh_faces, inmat=inmat_this,using_contactnet=using_contactnet)
