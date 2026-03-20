import numpy as np
import pyrealsense2 as rs
import os
import atexit
import time

class RSCapture:
    @staticmethod
    def get_available_devices_info():
        """获取所有可用设备的型号和序列号"""
        ctx = rs.context()
        devices = ctx.query_devices()
        return [f"{d.get_info(rs.camera_info.name)} (SN: {d.get_info(rs.camera_info.serial_number)})" for d in devices]

    @staticmethod
    def _get_lsusb_info():
        try:
            import subprocess
            res = subprocess.check_output("lsusb | grep Intel", shell=True).decode()
            return res.strip()
        except:
            return "无法获取 lsusb 信息"

    @staticmethod
    def get_device_serial_numbers():
        ctx = rs.context()
        # 显式尝试查询所有产品线
        devices = ctx.query_devices()
        serials = []
        for d in devices:
            try:
                sn = d.get_info(rs.camera_info.serial_number)
                serials.append(sn)
            except:
                pass
        return serials

    def __init__(self, name, serial_number, dim=(1280, 720), fps=30, depth=True):
        """
        """
        self.dim = dim
        self.name = name
        
        available_serials = RSCapture.get_device_serial_numbers()
        
        # 尝试忽略大小写和前导零的匹配
        target_sn = str(serial_number).strip().upper()
        matched_sn = None
        for s in available_serials:
            if s.upper().endswith(target_sn) or target_sn.endswith(s.upper()):
                matched_sn = s
                break
        
        if not matched_sn:
            devices_info = RSCapture.get_available_devices_info()
            raise ValueError(
                f"序列号 {serial_number} 未找到。\n"
                f"当前系统识别到的 RealSense 设备有: {devices_info}\n"
                f"操作系统 (lsusb) 识别到的设备: {RSCapture._get_lsusb_info()}\n"
            )
            
        self.serial_number = matched_sn
        self.depth = depth

        # 在启动前先获取设备型号
        ctx = rs.context()
        devices = ctx.query_devices()
        self.model = "Unknown"
        for d in devices:
            if d.get_info(rs.camera_info.serial_number) == self.serial_number:
                self.model = d.get_info(rs.camera_info.name)
                break

        self.pipe = rs.pipeline()
        self.cfg = rs.config()
        self.cfg.enable_device(self.serial_number)
        
        self.cfg.enable_stream(rs.stream.color, dim[0], dim[1], rs.format.bgr8, fps)
        if self.depth:
            if "L515" in self.model:
                d_dim = (1024, 768) if dim[0] >= 1024 else (640, 480)
                self.cfg.enable_stream(rs.stream.depth, d_dim[0], d_dim[1], rs.format.z16, fps)
            else:
                self.cfg.enable_stream(rs.stream.depth, dim[0], dim[1], rs.format.z16, fps)
            
        self.profile = self.pipe.start(self.cfg)
        
        # 确保在程序退出时能自动释放相机，防止占用
        atexit.register(self.close)

        device = self.profile.get_device()
        print(f"[{self.name}] 成功初始化 {self.model} (SN: {self.serial_number}, Type: {self.camera_type})")

        # 滤波器配置
        if self.depth:
            self.is_stereo = "D4" in self.model
            self.is_l515 = "L515" in self.model
            if self.is_stereo:
                self.depth_to_disparity = rs.disparity_transform(True)
                self.disparity_to_depth = rs.disparity_transform(False)
            
            self.spatial = rs.spatial_filter()
            self.temporal = rs.temporal_filter()
            self.hole_filling = rs.hole_filling_filter(2)
            
            depth_sensor = device.first_depth_sensor()
            if self.is_l515:
                if depth_sensor.supports(rs.option.confidence_threshold):
                    depth_sensor.set_option(rs.option.confidence_threshold, 1)
            elif self.is_stereo:
                if depth_sensor.supports(rs.option.emitter_enabled):
                    depth_sensor.set_option(rs.option.emitter_enabled, 1)
            self.depth_scale = depth_sensor.get_depth_scale()

        # 内参和对齐
        intrinsics = self.profile.get_stream(rs.stream.color).as_video_stream_profile().get_intrinsics()
        self.K = np.array([[intrinsics.fx, 0, intrinsics.ppx], [0, intrinsics.fy, intrinsics.ppy], [0, 0, 1]])
        self.align = rs.align(rs.stream.color)

    def get_intrinsics(self):
        """获取相机内参"""
        return self.K

    def read(self):
        frames = self.pipe.wait_for_frames()
        aligned_frames = self.align.process(frames)
        color_frame = aligned_frames.get_color_frame()
        depth_frame = aligned_frames.get_depth_frame()

        if not color_frame: return False, None, None

        if self.depth and depth_frame:
            if self.is_stereo:
                depth_frame = self.depth_to_disparity.process(depth_frame)
            depth_frame = self.spatial.process(depth_frame)
            depth_frame = self.temporal.process(depth_frame)
            if self.is_stereo:
                depth_frame = self.disparity_to_depth.process(depth_frame)
            depth_frame = self.hole_filling.process(depth_frame)

            color_image = np.asanyarray(color_frame.get_data())
            depth_image = np.asanyarray(depth_frame.get_data()).astype(np.float32)
            
            return True, color_image, depth_image * self.depth_scale
        
        return True, np.asanyarray(color_frame.get_data()), None

    def close(self):
        self.pipe.stop()

    def __del__(self):
        self.close()
