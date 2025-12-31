#!/usr/bin/env python3
import asyncio
import os
import json
import re
from pathlib import Path
from aiofiles.os import path as aiopath
from bot import LOGGER

class GPhoto:
    def __init__(self, dluploader, api_key):
        self.api_key = api_key
        self.dluploader = dluploader
        self.SIZE_LIMIT = 20 * 1024 * 1024
        
        # 分类定义后缀名
        self.VIDEO_EXTS = {
            '.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm', 
            '.3gp', '.ts', '.m4v', '.mpg', '.mpeg'
        }
        self.IMAGE_EXTS = {
            '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.heic', '.heif'
        }
        # 合并所有媒体后缀用于判断是否需要 gpd 转换
        self.MEDIA_EXTS = self.VIDEO_EXTS | self.IMAGE_EXTS

        # 两个不同的前缀地址
        self.BASE_URL_STILL = "https://photos.google.com/photo/"
        self.BASE_URL_VIDEO = "https://gphotoapi.playingapi.tech/?key="

    async def _execute(self, cmd):
        """执行系统命令并实时打印日志"""
        LOGGER.info(f"GPhoto 执行命令: {cmd}")
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        stdout_str = stdout.decode().strip()
        stderr_str = stderr.decode().strip()
        
        if process.returncode != 0:
            LOGGER.error(f"GPhoto 命令执行报错: {stderr_str}")
            return False, stderr_str
        return True, stdout_str

    def _extract_link(self, output, is_video=False):
        """精准提取 mediakey，根据文件类型选择前缀"""
        try:
            match = re.search(r'\{.*\}', output, re.DOTALL)
            if not match:
                LOGGER.error("GPhoto: 输出中未找到 JSON 内容")
                return None
            
            data = json.loads(match.group())
            if "results" in data and len(data["results"]) > 0:
                res = data["results"][0]
                
                # 大小写全兼容处理
                lowered_res = {k.lower(): v for k, v in res.items()}
                m_key = lowered_res.get("mediakey")
                success_flag = res.get("success") or lowered_res.get("success")
                
                if success_flag and m_key:
                    m_key = m_key.strip()
                    # 根据类型选择前缀
                    if is_video:
                        final_link = f"{self.BASE_URL_VIDEO}{m_key}"
                    else:
                        base = self.BASE_URL_STILL.rstrip('/')
                        final_link = f"{base}/{m_key}"
                    
                    LOGGER.info(f"GPhoto 成功生成[{'视频' if is_video else '图片'}]链接: {final_link}")
                    return final_link
            
            LOGGER.error(f"GPhoto: JSON 中未找到 mediakey。Data: {data}")
            return None
        except Exception as e:
            LOGGER.error(f"GPhoto 解析异常: {str(e)}")
            return None

    async def upload_file(self, file_path):
        """处理单文件：授权校验 -> 隐写转换 -> 类型判断 -> 上传"""
        if self.dluploader.is_cancelled:
            return None

        path = Path(file_path)
        ext = path.suffix.lower()
        upload_path, is_converted = str(file_path), False
        
        # 初始判断是否为视频
        is_video = ext in self.VIDEO_EXTS

        # 1. 智能授权：检查当前是否有激活账号
        success_list, out_list = await self._execute("gotohp creds list")
        active_user = None
        if success_list:
            for line in out_list.split('\n'):
                if '*' in line:
                    active_user = line.replace('*', '').strip()
                    break
        
        if active_user:
            LOGGER.info(f"GPhoto: 当前验证的用户: {active_user}")
        elif self.api_key:
            LOGGER.info("GPhoto: 未发现认证用户，正在执行 gotohp creds add...")
            auth_ok, auth_err = await self._execute(f"gotohp creds add '{self.api_key}'")
            if not auth_ok:
                return None
        else:
            LOGGER.warn("GPhoto: 无认证用户且未配置 API Key")

        # 2. 预处理 (gpd hide)
        if ext not in self.MEDIA_EXTS:
            filesize = os.path.getsize(file_path)
            LOGGER.info(f"GPhoto: 正在转换非媒体文件: {path.name}")
            if filesize < self.SIZE_LIMIT:
                # 转换成 BMP (图片前缀)
                cmd = f"gpd hide '{file_path}'"
                is_video = False
                suffix = ".bmp"
            else:
                # 转换成 MP4 (视频前缀)
                cmd = f"gpd hide -t video '{file_path}'"
                is_video = True
                suffix = ".mp4"
            
            success, _ = await self._execute(cmd)
            if success:
                upload_path = f"{file_path}{suffix}"
                is_converted = True
                LOGGER.info(f"GPhoto: 转换完成 -> {upload_path} (类型: {'视频' if is_video else '图片'})")

        # 3. 执行上传
        LOGGER.info(f"GPhoto: 启动上传任务...")
        success_up, output_up = await self._execute(f"gotohp upload '{upload_path}' -r -d -t 5")
        
        if success_up:
            final_link = self._extract_link(output_up, is_video=is_video)
            if final_link:
                try:
                    self.dluploader._DDLUploader__processed_bytes += os.path.getsize(file_path)
                except: pass
                
                # 清理逻辑
                if is_converted and os.path.exists(file_path):
                    try: os.remove(file_path)
                    except: pass
                
                self.dluploader.total_files += 1
                return final_link
        
        # 失败处理
        if is_converted and os.path.exists(upload_path):
            try: os.remove(upload_path)
            except: pass
        return None

    async def upload(self, path):
        """主入口"""
        LOGGER.info(f"GPhoto 任务启动，路径: {path}")
        
        if await aiopath.isfile(path):
            result = await self.upload_file(path)
            if result and str(result).startswith("http"):
                return result
            raise Exception("GPhoto 上传失败或解析链接非法。")
        
        links = []
        for root, _, files in os.walk(path):
            for file in files:
                if self.dluploader.is_cancelled:
                    return "上传已取消"
                file_full_path = os.path.join(root, file)
                link = await self.upload_file(file_full_path)
                if link:
                    links.append(f"<code>{file}</code>: {link}")
        
        return "\n".join(links) if links else "处理失败"
