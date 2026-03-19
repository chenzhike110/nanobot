from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from PIL import Image, ImageColor, ImageDraw


def load_image(path: str) -> np.ndarray:
    img = cv2.imread(path)
    if img is None:
        raise ValueError(f"load_image: load image from '{path[:60]}…'")
    return img


def load_depth(path: str) -> np.ndarray:
    depth = np.load(path)
    if depth is None:
        raise ValueError(f"load_depth: load depth from '{path[:60]}…'")
    return depth


def encode_image(image):
    """将图像编码为 base64 字符串 (JPG 格式)"""
    _, buffer = cv2.imencode('.jpg', image)
    return base64.b64encode(buffer).decode('utf-8')


def encode_depth(depth: np.ndarray) -> str:
    """编码深度图为 PNG Base64 (配合服务端的 Image.open)"""
    # 确保深度图是 uint16 (毫米)，这是深度图 PNG 的标准格式
    if depth.dtype == np.float32:
        # 如果输入是米 (float32)，转回毫米 (uint16)
        depth_to_save = (depth * 1000).astype(np.uint16)
    else:
        depth_to_save = depth.astype(np.uint16)
    
    # 使用 PNG 编码，因为 PNG 是无损的且支持 uint16
    _, buffer = cv2.imencode('.png', depth_to_save)
    return base64.b64encode(buffer).decode('utf-8')


def decode_base64_file(base64_str):
    """Decode a base64 string back to binary data."""
    return base64.b64decode(base64_str)


def decode_mask(mask_b64: str, target_shape: Optional[tuple] = None) -> np.ndarray:
    """
    解码base64编码的mask为numpy数组
    target_shape: (height, width) 如果提供，会resize到该尺寸
    """
    mask_data = base64.b64decode(mask_b64)
    nparr = np.frombuffer(mask_data, np.uint8)
    mask_rgba = cv2.imdecode(nparr, cv2.IMREAD_UNCHANGED)
    
    if mask_rgba is None:
        raise ValueError("Failed to decode mask")
    
    # Extract alpha channel as binary mask
    if mask_rgba.ndim == 3 and mask_rgba.shape[2] >= 4:
        binary_mask = (mask_rgba[:, :, 3] > 0).astype(np.uint8)
    else:
        # 如果是灰度图，直接使用
        binary_mask = (mask_rgba > 0).astype(np.uint8)
    
    # Resize if target shape is provided
    if target_shape is not None:
        binary_mask = cv2.resize(binary_mask, (target_shape[1], target_shape[0]), 
                                 interpolation=cv2.INTER_NEAREST)
    
    return binary_mask


def visualize_mask_on_image(image, mask, alpha=0.5):
    """
    在图像上可视化mask
    """
    if mask is None or mask.size == 0:
        return image.copy()
    
    # 创建彩色mask
    colored_mask = np.zeros_like(image)
    colored_mask[mask > 0] = [0, 255, 0]  # 绿色
    
    # 混合原图和mask
    result = cv2.addWeighted(image, 1 - alpha, colored_mask, alpha, 0)
    
    # 绘制mask边界
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(result, contours, -1, (0, 255, 0), 2)
    
    return result


def draw_coordinate_frame(image: np.ndarray, pose: np.ndarray, K: np.ndarray, axis_length: float = 0.1, thickness: int = 3) -> np.ndarray:
    """
    在图像上绘制3D坐标轴（X红、Y绿、Z蓝）
    
    Args:
        image: 输入图像
        pose: 4x4变换矩阵（在相机坐标系下）
        K: 相机内参矩阵
        axis_length: 坐标轴长度（米）
        thickness: 线条粗细
    
    Returns:
        绘制后的图像
    """
    # 定义坐标轴端点（在局部坐标系下）
    points = np.float32([
        [0, 0, 0],           # 原点
        [axis_length, 0, 0], # X轴
        [0, axis_length, 0], # Y轴
        [0, 0, axis_length]  # Z轴
    ]).reshape(-1, 3)
    
    # 将旋转矩阵转换为旋转向量
    rvec, _ = cv2.Rodrigues(pose[:3, :3])
    tvec = pose[:3, 3]
    
    # 投影3D点到2D图像平面
    imgpts, _ = cv2.projectPoints(points, rvec, tvec, K, None)
    imgpts = imgpts.astype(int)
    
    origin = tuple(imgpts[0].ravel())
    # X轴：红色 (BGR格式)
    image = cv2.line(image, origin, tuple(imgpts[1].ravel()), (0, 0, 255), thickness)
    # Y轴：绿色 (BGR格式)
    image = cv2.line(image, origin, tuple(imgpts[2].ravel()), (0, 255, 0), thickness)
    # Z轴：蓝色 (BGR格式)
    image = cv2.line(image, origin, tuple(imgpts[3].ravel()), (255, 0, 0), thickness)
    
    return image

