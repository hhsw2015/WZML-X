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
        self.dluploader = dluploader  # 引用 DDLUploader 实例
        self.SIZE_LIMIT = 20 * 1024 * 1024
        self.MEDIA_EXTS = {
            '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.heic', '.heif',
            '.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm'
        }
        # 已修正为 /3
        self.BASE_URL = "https://photos.google.com/photo/"

    async def _execute(self, cmd):
        """异步执行系统命令"""
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return (process.returncode == 0, stdout.decode().strip() if process.returncode == 0 else stderr.decode().strip())

    def _extract_link(self, output):
        """精准提取 mediaKey 并拼接最终链接"""
        try:
            # 使用正则匹配最外层的 JSON 结构，防止 gotohp 前缀文字干扰
            match = re.search(r'\{.*\}', output, re.DOTALL)
            if not match:
                return output
            
            data = json.loads(match.group())
            if "results" in data and len(data["results"]) > 0:
                media_key = data["results"][0].get("mediaKey")
                if media_key:
                    return f"{self.BASE_URL}{media_key}"
            return output
        except Exception as e:
            LOGGER.error(f"GPhoto 解析错误: {e}")
            return output

    async def upload_file(self, file_path):
        """单文件处理逻辑：授权 -> 转换 -> 上传 -> 清理"""
        if self.dluploader.is_cancelled:
            return None

        path = Path(file_path)
        upload_path, is_converted = str(file_path), False

        # 1. 预授权：执行 gotohp creds add
        if self.api_key:
            auth_ok, auth_err = await self._execute(f'gotohp creds add "{self.api_key}"')
            if not auth_ok:
                LOGGER.error(f"GPhoto 授权失败: {auth_err}")
                return f"Auth Error: {auth_err}"

        # 2. 预处理 (gpd hide)
        if path.suffix.lower() not in self.MEDIA_EXTS:
            filesize = os.path.getsize(file_path)
            cmd = f"gpd hide '{file_path}'" if filesize < self.SIZE_LIMIT else f"gpd hide -t video '{file_path}'"
            success, _ = await self._execute(cmd)
            if success:
                upload_path = f"{file_path}.bmp" if filesize < self.SIZE_LIMIT else f"{file_path}.mp4"
                is_converted = True

        # 3. 上传 (gotohp)
        LOGGER.info(f"GPhoto: 正在上传: {Path(upload_path).name}")
        # -d 成功后自动删除伪装件 (upload_path)
        success, output = await self._execute(f"gotohp upload '{upload_path}' -r -d -t 5")
        
        if success:
            # 手动更新引擎统计信息 (进度显示)
            try:
                processed_size = os.path.getsize(file_path)
                self.dluploader._DDLUploader__processed_bytes += processed_size
            except: pass
            
            # 成功后清理最初原件
            if is_converted and os.path.exists(file_path):
                try: os.remove(file_path)
                except: pass
            
            self.dluploader.total_files += 1
            return self._extract_link(output)
        else:
            # 失败后清理伪装件，保留原件
            if is_converted and os.path.exists(upload_path):
                try: os.remove(upload_path)
                except: pass
            return None

    async def upload(self, path):
        """入口方法：适配 DDLUploader 调用"""
        if await aiopath.isfile(path):
            return await self.upload_file(path) or "上传失败"
        
        links = []
        for root, _, files in os.walk(path):
            for file in files:
                if self.dluploader.is_cancelled:
                    return "已取消"
                link = await self.upload_file(os.path.join(root, file))
                if link:
                    links.append(f"<code>{file}</code>: {link}")
        return "\n".join(links) if links else "上传失败"
