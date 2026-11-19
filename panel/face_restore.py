# -*- coding: utf-8 -*-
"""GFPGAN 人脸修复(照片清晰化链路的第一步, 只修脸不动背景)
用法: python face_restore.py <输入图> <输出.jpg> [保真权重0~1, 默认0.5]
权重越大越像原图(糊一点), 越小越清晰(可能变样); 0.5 是平衡档
"""
import os
import sys

import cv2
import torch

import gfpgan

WEIGHTS = os.path.join(os.path.dirname(os.path.abspath(gfpgan.__file__)), 'weights', 'GFPGANv1.4.pth')


def main():
    src, dst = sys.argv[1], sys.argv[2]
    w = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    from gfpgan import GFPGANer
    er = GFPGANer(model_path=WEIGHTS, upscale=1, arch='clean', channel_multiplier=2,
                  bg_upsampler=None, device=device)
    img = cv2.imread(src, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise SystemExit('读不到图片: ' + src)
    _, restored, _ = er.enhance(img, has_aligned=False, only_center_face=False,
                                paste_back=True, weight=w)
    cv2.imwrite(dst, restored[0], [cv2.IMWRITE_JPEG_QUALITY, 95])
    print('face restored ->', dst)


if __name__ == '__main__':
    main()
