import os
import torch
import numpy as np
import imageio
import config
from easydict import EasyDict
from transformers.texformer import Texformer
from RSC_net.ra_test import RaRunner
from NMR.neural_render_test import NrTextureRenderer
from utils.scipy_deprecated import imresize
import matplotlib.pyplot as plt
from tqdm import tqdm

from psbody.mesh import Mesh


class Demo:
    def __init__(self):
        self.device = 'cuda'
        self.opts = EasyDict(src_ch=4, tgt_ch=3, feat_dim=128,
                             nhead=8, mask_fusion=1, out_ch=3,
                             checkpoint_path='./pretrained/texformer_ep500.pt')
        self.checkpoint_path = self.opts.checkpoint_path
        
        self.model = Texformer(self.opts)
        self.model.load_state_dict(torch.load(self.checkpoint_path))
        self.model.eval()
        self.model.to(self.device)

        self.tgt = torch.from_numpy(np.load(config.uv_encoding_path)).permute(2, 0, 1)[None]
        self.tgt = (self.tgt * 2 -1).float().to(self.device)

        # others
        self.rsc_runner = RaRunner()
        self.renderer_seg = NrTextureRenderer(224)
        self.renderer = NrTextureRenderer(224, factor=0.8)
        self.smpl_part_seg_mapping = torch.tensor([0, 3, 3, 1, 5, 5, 2, 4, 4, 6, 6, 7, 7]).long()
    
    def preprocess_img(self, img):
        img = img / 255.
        img_tensor = torch.from_numpy(img).float().permute(2, 0, 1).unsqueeze(0)
        return img_tensor
    
    def get_smpl(self, img_tensor):
        scale = self.rsc_runner.get_scale(128)
        with torch.no_grad():
            # img_tensor is sent into GPU in runner
            pred_vertices, pred_cam_t, pred_rotmat, pred_betas = self.rsc_runner.get_3D_batch(img_tensor, scale, pre_process=True)
        self.pred_vertices = pred_vertices
        self.pred_cam_t = pred_cam_t
    
    def get_segmentation(self):
        parts = self.renderer_seg.render_part(self.pred_vertices, self.pred_cam_t, crop_width=None)
        parts = parts.cpu().long()      
        parts = self.smpl_part_seg_mapping[parts]
        return parts
    
    def resize_224(self, img):
        h, w = img.shape[0:2]

        if h > w:
            h_target = 224
            ratio = h_target / h 
            w_target = int(ratio * w)
        else:
            w_target = 224
            ratio = w_target / w 
            h_target = int(ratio * w)

        img = imresize(img, [h_target, w_target])

        diff = h_target - w_target
        before = abs(diff) // 2
        after = diff - before
        if diff > 0:
            # padding
            img = np.pad(img, [(0, 0), (before, after), (0, 0)])
        elif diff < 0:
            # crop
            img = img[:, before:- after]
        return img
    
    def preprocess_for_texture(self, img_tensor, parts):
        parts = parts.unsqueeze(1).float() / 7.0    # [1, 1, 224, 224], float, [0, 1]
        h, w = parts.shape[2:]
        h_target = 128
        w_target = h_target * w / h 
        
        if h != 128:
            parts = torch.nn.functional.interpolate(parts, [int(h_target), int(w_target)], mode='nearest')
        if w_target > 64:
            start = int((w_target-64) // 2)
            parts = parts[:, :, :, start:start+64]
        elif w_target < 64:
            before = (64-w_target) // 2
            after = 64-w_target-before
            parts = torch.nn.functional.pad(parts, [before, after])
        parts = parts * 2 - 1
        parts = parts.to(self.device)
        
        img_tensor = img_tensor * 2 - 1
        img_tensor = torch.nn.functional.interpolate(img_tensor, [128, 128])
        img_tensor = img_tensor[:, :, :, 32:32+64].to(self.device)

        return img_tensor, parts
    
    def get_coord(self, shape):
        y = np.linspace(-1.0, 1.0, num=shape[0])
        x = np.linspace(-1.0, 1.0, num=shape[1])
        coord_y, coord_x = np.meshgrid(y, x, indexing='ij')
        coord = np.concatenate((coord_y[None], coord_x[None]), axis=0)
        return torch.from_numpy(coord).float()

    def get_texture(self, img, seg):
        coord = self.get_coord([128, 64]).unsqueeze(0).to(self.device)
        value = torch.cat([coord, img], dim=1)
        src = torch.cat([img, seg], dim=1)
        out = self.model(self.tgt, src, value)
        tex_flow = out[0]
        # uvmap_rgb = out[1]
        # combine_mask = out[2]
        uvmap_flow = torch.nn.functional.grid_sample(img, tex_flow.permute(0, 2, 3, 1))
        # uvmap = uvmap_flow * combine_mask + uvmap_rgb * (1-combine_mask)
        uvmap = uvmap_flow

        return uvmap
    
    @torch.no_grad()
    def run_demo(self, args):
        
        images = []
        segmentations = []
        if os.path.isdir(args.img_path):
            
            assert (args.seg_path is None or os.path.isdir(args.seg_path))

            save_path = args.img_path
            
            for f in os.listdir(args.img_path):
                f_path = os.path.join(args.img_path, f)
                f_name = ".".join(f.split(".")[:-1])
                if os.path.isfile(f_path):
                    images.append(f_path)
                    
                    if args.seg_path is not None:
                        segmentations.append(os.path.join(args.seg_path, f_name + "_seg.png"))
                    else:
                        segmentations.append(None)
        else:
            save_path, _ = os.path.split(args.img_path)
            images = [args.img_path]
            segmentations = [args.seg_path]

        if args.save_path is not None:
            save_path = args.save_path
        os.makedirs(save_path, exist_ok=True)
        
        with tqdm(total = len(images)) as progress_bar:
            for img_path, seg_path in zip(images, segmentations):
                img_dir, img_name = os.path.split(img_path)
                img_ext = img_name.split(".")[-1]
                img_name = ".".join(img_name.split(".")[:-1])

                img = imageio.imread(img_path)

                try:
                    img_224 = self.resize_224(img)
                
                    img_tensor = self.preprocess_img(img_224)
                    self.get_smpl(img_tensor)
                except RuntimeError:
                    continue
                
                if seg_path is not None:
                    seg = imageio.imread(seg_path)
                    parts = torch.from_numpy(seg)[None].long()
                else:
                    parts = self.get_segmentation()

                img_tensor, parts = self.preprocess_for_texture(img_tensor, parts)    # ~[-1, 1]
                uvmap = self.get_texture(img_tensor, parts)
                uvmap = (uvmap + 1) / 2
                uvmap = uvmap.clamp(0, 1)

                v = np.squeeze(self.pred_vertices.cpu().numpy()).astype(np.float64)
                f = self.renderer.faces.cpu().numpy().astype(np.uint32)
                cams = np.squeeze(self.pred_cam_t.cpu().numpy())
                cams *= np.array([1, 1, -1])
                cams = cams.astype(np.float64).tolist()
               
                # Compute partial visibility
                mesh = Mesh(
                    filename = "smpl_uv.obj",
                )
                mesh.v = v
                mesh.f = f
                mesh._texture_image = np.squeeze(uvmap).cpu().permute(1, 2, 0).numpy()
                _, partial_texture = mesh.visibile_mesh(
                    camera=cams,
                    return_texture=True,
                    color=(255, 255, 255),
                    criterion=np.all,
                )
                partial_texture = torch.from_numpy(partial_texture).to(uvmap.device)
                partial_texture = partial_texture.permute(2, 0, 1).reshape(uvmap.shape)
                # partial_texture = partial_texture.permute(2, 0, 1)[None, :, :, :]

                rendered_img, depth, mask = self.renderer.render(
                    self.pred_vertices,
                    self.pred_cam_t,
                    partial_texture,
                )
                rendered_img = rendered_img[0].cpu().permute(1, 2, 0).numpy()
                depth = depth.cpu().permute(1, 2, 0).numpy()
                mask = mask.cpu().permute(1, 2, 0).numpy()

                # rendered_img_rot = self.renderer.render(self.pred_vertices, self.pred_cam_t, uvmap, euler=[0, 180, 0])
                rendered_img_rot, depth_rot, mask_rot = self.renderer.render(
                    self.pred_vertices,
                    self.pred_cam_t,
                    partial_texture,
                    euler=[0, 180, 0]
                )
                rendered_img_rot = rendered_img_rot[0].cpu().permute(1, 2, 0).numpy()
                depth_rot = depth_rot.cpu().permute(1, 2, 0).numpy()
                mask_rot = mask_rot.cpu().permute(1, 2, 0).numpy()

                uvmap = np.squeeze(uvmap).cpu().permute(1, 2, 0).numpy()
                partial_texture = np.squeeze(partial_texture).cpu().permute(1, 2, 0).numpy()

                figure, axes = plt.subplots(1, 4, figsize=(20, 6))
                axes[0].imshow(img); axes[0].axis('off'); axes[0].set_title('input')
                axes[1].imshow(partial_texture); axes[1].axis('off'); axes[1].set_title('partial uvmap')
                axes[2].imshow(rendered_img); axes[1].axis('off'); axes[2].set_title('rendered')
                axes[3].imshow(rendered_img_rot); axes[2].axis('off'); axes[3].set_title('rotated')
                
                plt.savefig(os.path.join(
                    save_path,
                    "{}_texture.{}".format(img_name, img_ext))
                )
                plt.close()
                progress_bar.update()


if __name__ == '__main__':
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument('--img_path', required=True, help='Please specify the input image path')
    parser.add_argument('--seg_path', type=str, default=None, help='Human part segmentation path. If None, use the result of RSC-Net instead')
    parser.add_argument('--save', action='store_true', default=False, help='Whether save the output figure?')
    parser.add_argument('--save_path', default=None, help='Path to save the output images')

    args = parser.parse_args()

    demo = Demo()
    demo.run_demo(args)
    print("Run demo OK")
