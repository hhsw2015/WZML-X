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
        self.MEDIA_EXTS = {
            '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.heic', '.heif',
            '.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm'
        }
        # 确认使用 /3
        self.BASE_URL = "https://photos.google.com/photo/"

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

    def _extract_link(self, output):
        """精准提取 mediakey，大小写全兼容"""
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
                    base = self.BASE_URL.rstrip('/')
                    final_link = f"{base}/{m_key.strip()}".strip()
                    LOGGER.info(f"GPhoto 成功解析链接: {final_link}")
                    return final_link
            
            LOGGER.error(f"GPhoto: JSON 成功解析但未找到有效 mediakey。Data: {data}")
            return None
        except Exception as e:
            LOGGER.error(f"GPhoto 解析异常: {str(e)}")
            return None

    async def upload_file(self, file_path):
        """处理单文件流程"""
        if self.dluploader.is_cancelled:
            return None

        path = Path(file_path)
        upload_path, is_converted = str(file_path), False

        # 1. 智能授权逻辑
        success_list, out_list = await self._execute("gotohp creds list")
        active_user = None
        if success_list:
            for line in out_list.split('\n'):
                if '*' in line: # 寻找激活标记
                    active_user = line.replace('*', '').strip()
                    break
        
        if active_user:
            LOGGER.info(f"GPhoto: 当前已有激活用户 [{active_user}]，跳过授权。")
        elif self.api_key:
            LOGGER.info("GPhoto: 未发现认证用户，正在执行 gotohp creds add...")
            # 只有没有认证用户时才调用 add
            auth_ok, auth_err = await self._execute(f"gotohp creds add '{self.api_key}'")
            if not auth_ok:
                LOGGER.error(f"GPhoto 自动授权失败: {auth_err}")
                return None
        else:
            LOGGER.warn("GPhoto: 无认证用户且未配置 API Key，上传可能失败。")

        # 2. 预处理 (gpd hide)
        if path.suffix.lower() not in self.MEDIA_EXTS:
            filesize = os.path.getsize(file_path)
            LOGGER.info(f"GPhoto: 正在转换非媒体文件: {path.name}")
            cmd = f"gpd hide '{file_path}'" if filesize < self.SIZE_LIMIT else f"gpd hide -t video '{file_path}'"
            success, _ = await self._execute(cmd)
            if success:
                upload_path = f"{file_path}.bmp" if filesize < self.SIZE_LIMIT else f"{file_path}.mp4"
                is_converted = True
                LOGGER.info(f"GPhoto: 转换完成，新路径: {upload_path}")

        # 3. 调用上传 (gotohp)
        LOGGER.info(f"GPhoto: 准备调用 gotohp upload 上传任务...")
        success_up, output_up = await self._execute(f"gotohp upload '{upload_path}' -r -d -t 5")
        
        if success_up:
            final_link = self._extract_link(output_up)
            if final_link:
                # 更新进度字节统计
                try:
                    self.dluploader._DDLUploader__processed_bytes += os.path.getsize(file_path)
                except: pass
                
                # 成功后清理最初原件
                if is_converted and os.path.exists(file_path):
                    try: os.remove(file_path)
                    except: pass
                
                self.dluploader.total_files += 1
                return final_link
        
        # 失败处理：清理产生的临时伪装文件
        if is_converted and os.path.exists(upload_path):
            try: os.remove(upload_path)
            except: pass
        return None

    async def upload(self, path):
        """入口方法：处理文件或文件夹"""
        LOGGER.info(f"GPhoto 任务启动，路径: {path}")
        
        if await aiopath.isfile(path):
            result = await self.upload_file(path)
            if result and str(result).startswith("http"):
                return result
            # 抛出异常防止生成无效的 Telegram 按钮
            raise Exception("GPhoto 上传失败或无法解析链接，详情请查看日志。")
        
        # 文件夹递归处理
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
