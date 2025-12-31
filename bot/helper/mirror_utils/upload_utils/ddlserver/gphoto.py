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
        # 修正后的 BASE_URL
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
            LOGGER.error(f"GPhoto 命令报错: {stderr_str}")
            return False, stderr_str
        return True, stdout_str

    def _extract_link(self, output):
        """精准提取 mediakey，兼容大小写，确保返回纯净 URL"""
        try:
            # 使用正则匹配 JSON 内容
            match = re.search(r'\{.*\}', output, re.DOTALL)
            if not match:
                LOGGER.error("GPhoto: 输出中未找到 JSON 内容")
                return None
            
            data = json.loads(match.group())
            if "results" in data and len(data["results"]) > 0:
                res = data["results"][0]
                
                # --- 大小写全兼容处理 ---
                lowered_res = {k.lower(): v for k, v in res.items()}
                m_key = lowered_res.get("mediakey")
                success_flag = res.get("success") or lowered_res.get("success")
                
                if success_flag and m_key:
                    base = self.BASE_URL.rstrip('/')
                    # 清洗 Key 中的空格或换行
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

        # 1. 授权查询逻辑：执行 gotohp creds list 并显示激活用户
        success_list, out_list = await self._execute("gotohp creds list")
        if success_list:
            active_user = "未找到激活账号"
            # 寻找带 * 的行，例如: * playingapi@gmail.com
            for line in out_list.split('\n'):
                if '*' in line and '@' in line:
                    active_user = line.replace('*', '').strip()
                    break
            LOGGER.info(f"GPhoto: 当前验证的用户: {active_user}")
        else:
            LOGGER.warn(f"GPhoto: 无法获取授权列表。")

        # 2. 预处理 (gpd hide)
        if path.suffix.lower() not in self.MEDIA_EXTS:
            filesize = os.path.getsize(file_path)
            LOGGER.info(f"GPhoto: 正在转换非媒体文件: {path.name}")
            cmd = f"gpd hide '{file_path}'" if filesize < self.SIZE_LIMIT else f"gpd hide -t video '{file_path}'"
            success, _ = await self._execute(cmd)
            if success:
                upload_path = f"{file_path}.bmp" if filesize < self.SIZE_LIMIT else f"{file_path}.mp4"
                is_converted = True
                LOGGER.info(f"GPhoto: 转换完成，上传路径: {upload_path}")

        # 3. 调用上传 (gotohp)
        LOGGER.info(f"GPhoto: 启动 gotohp 上传任务...")
        success_up, output_up = await self._execute(f"gotohp upload '{upload_path}' -r -d -t 5")
        
        if success_up:
            final_link = self._extract_link(output_up)
            if final_link:
                # 更新进度字节统计
                try:
                    self.dluploader._DDLUploader__processed_bytes += os.path.getsize(file_path)
                except: pass
                
                # 成功后清理最初原件 (如果转换过)
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
        LOGGER.info(f"GPhoto 任务启动，处理路径: {path}")
        
        if await aiopath.isfile(path):
            result = await self.upload_file(path)
            if result and str(result).startswith("http"):
                return result
            # 抛出异常防止引擎生成无效的 Telegram 按钮链接
            raise Exception("GPhoto 上传失败或无法解析链接，请检查日志。")
        
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
