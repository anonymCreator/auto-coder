"""
活动上下文管理器 - 子系统的主要接口
"""

import os
import sys
import time
import threading
from datetime import datetime
from typing import List, Dict, Optional, Any, Tuple, Set
from loguru import logger
import byzerllm
import re
from pydantic import BaseModel, Field

from autocoder.common import AutoCoderArgs
from autocoder.common.action_yml_file_manager import ActionYmlFileManager
from autocoder.common.printer import Printer
from autocoder.memory.directory_mapper import DirectoryMapper
from autocoder.memory.active_package import ActivePackage
from autocoder.memory.async_processor import AsyncProcessor


class ActiveFileSections(BaseModel):
    """活动文件内容的各个部分"""
    header: str = Field(default="", description="文件标题部分")
    current_change: str = Field(default="", description="当前变更部分")
    document: str = Field(default="", description="文档部分")


class ActiveFileContext(BaseModel):
    """单个活动文件的上下文信息"""
    directory_path: str = Field(..., description="目录路径")
    active_md_path: str = Field(..., description="活动文件路径")
    content: str = Field(..., description="文件完整内容")
    sections: ActiveFileSections = Field(..., description="文件内容各部分")
    files: List[str] = Field(default_factory=list, description="与该活动文件相关的源文件列表")


class FileContextsResult(BaseModel):
    """文件上下文查询结果"""
    contexts: Dict[str, ActiveFileContext] = Field(
        default_factory=dict, 
        description="键为目录路径，值为活动文件上下文"
    )
    not_found_files: List[str] = Field(
        default_factory=list,
        description="未找到对应活动上下文的文件列表"
    )


class ActiveContextManager:
    """
    ActiveContextManager是活动上下文跟踪子系统的主要接口。
    
    该类负责：
    1. 从YAML文件加载任务数据
    2. 映射目录结构
    3. 生成活动上下文文档
    4. 管理异步任务执行
    """
    
    def __init__(self, llm: byzerllm.ByzerLLM, args: AutoCoderArgs):
        """
        初始化活动上下文管理器
        
        Args:
            llm: ByzerLLM实例，用于生成文档内容
            args: AutoCoderArgs实例，包含配置信息
        """
        self.llm = llm
        self.args = args
        self.directory_mapper = DirectoryMapper()
        self.active_package = ActivePackage(llm)
        self.async_processor = AsyncProcessor()
        self.yml_manager = ActionYmlFileManager(args.source_dir)
        self.tasks = {}  # 用于跟踪任务状态
        self.printer = Printer()
        
    def _redirect_output_to_file(self, func, *args, **kwargs):
        """
        将函数的所有输出重定向到日志文件
        
        Args:
            func: 要执行的函数
            *args, **kwargs: 传递给函数的参数
        """
        # 确保日志目录存在
        log_dir = os.path.join(self.args.source_dir, '.auto-coder', 'active-context')
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, 'active.log')
        
        # 保存原始的标准输出和标准错误
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        
        try:
            # 打开日志文件并重定向输出
            with open(log_file, 'a', encoding='utf-8') as f:
                # 添加时间戳
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n\n--- 开始执行任务 {timestamp} ---\n")
                
                # 重定向标准输出和标准错误
                sys.stdout = f
                sys.stderr = f
                
                # 配置loguru将日志输出到文件
                logger_id = logger.add(f, format="{time} {level} {message}", level="INFO")
                
                # 执行函数
                result = func(*args, **kwargs)
                
                # 添加结束时间戳
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n--- 任务执行完成 {timestamp} ---\n")
                
                return result
        finally:
            # 恢复原始的标准输出和标准错误
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            
            # 移除loguru的处理器
            try:
                logger.remove(logger_id)
            except:
                pass
    
    def process_changes(self, file_name: Optional[str] = None) -> str:
        """
        处理代码变更，创建活动上下文
        
        Args:
            file_name: YAML文件名，如果为None则使用args.file
        
        Returns:
            str: 任务ID，可用于后续查询任务状态
        """
        try:
            # 使用参数中的文件或者指定的文件
            file_name = file_name or os.path.basename(self.args.file)
            
            # 从YAML文件加载数据
            yaml_content = self.yml_manager.load_yaml_content(file_name)
            
            # 提取需要的信息
            query = yaml_content.get('query', '')
            changed_urls = yaml_content.get('add_updated_urls', [])
            current_urls = yaml_content.get('urls', []) + yaml_content.get('dynamic_urls', [])
            
            # 创建任务ID
            task_id = f"active_context_{int(time.time())}_{file_name}"
            
            # 更新任务状态
            self.tasks[task_id] = {
                'status': 'starting',
                'start_time': datetime.now(),
                'file_name': file_name,
                'query': query,
                'changed_urls': changed_urls,
                'current_urls': current_urls
            }
            
            # 创建并启动独立线程，将输出重定向到日志文件
            thread = threading.Thread(
                target=self._redirect_output_to_file,
                args=(self._process_changes_async, task_id, query, changed_urls, current_urls),
                daemon=True
            )
            thread.start()
            
            self.printer.print_in_terminal(
                "active_context_started", 
                task_id=task_id,
                style="green"
            )
            
            return task_id
        
        except Exception as e:
            logger.error(f"Error in process_changes: {e}")
            self.printer.print_in_terminal(
                "active_context_error", 
                error=str(e),
                style="red"
            )
            raise
    
    def _process_changes_async(self, task_id: str, query: str, changed_urls: List[str], current_urls: List[str]):
        """
        实际处理变更的异步方法
        
        Args:
            task_id: 任务ID
            query: 用户查询/需求
            changed_urls: 变更的文件路径列表
            current_urls: 当前相关的文件路径列表
        """
        try:
            self.tasks[task_id]['status'] = 'running'
            logger.info(f"Processing task {task_id}: {len(changed_urls)} changed files")
            
            # 获取当前任务的文件名
            file_name = self.tasks[task_id].get('file_name')
            
            # 获取文件变更信息
            file_changes = {}
            if file_name:
                commit_changes = self.yml_manager.get_commit_changes(file_name)
                if commit_changes and len(commit_changes) > 0:
                    # commit_changes结构为 [(query, urls, changes)]
                    _, _, changes = commit_changes[0]
                    file_changes = changes
                    logger.info(f"Retrieved {len(file_changes)} file changes from commit")
            
            # 1. 映射目录
            directory_contexts = self.directory_mapper.map_directories(
                self.args.source_dir, changed_urls, current_urls
            )
            
            # 2. 处理每个目录
            processed_dirs = []
            for context in directory_contexts:
                dir_path = context['directory_path']
                self._process_directory_context(context, query, file_changes)
                processed_dirs.append(os.path.basename(dir_path))
                
            # 3. 更新任务状态
            self.tasks[task_id]['status'] = 'completed'
            self.tasks[task_id]['completion_time'] = datetime.now()
            self.tasks[task_id]['processed_dirs'] = processed_dirs
            
            logger.info(f"Task {task_id} completed: processed {len(processed_dirs)} directories")
        
        except Exception as e:
            # 记录错误
            logger.error(f"Task {task_id} failed: {e}")
            self.tasks[task_id]['status'] = 'failed'
            self.tasks[task_id]['error'] = str(e)
    
    def _process_directory_context(self, context: Dict[str, Any], query: str, file_changes: Dict[str, Tuple[str, str]] = None):
        """
        处理单个目录上下文
        
        Args:
            context: 目录上下文字典
            query: 用户查询/需求
            file_changes: 文件变更字典，键为文件路径，值为(变更前内容, 变更后内容)的元组
        """
        try:
            directory_path = context['directory_path']
            
            # 1. 确保目录存在
            target_dir = self._get_active_context_path(directory_path)
            os.makedirs(target_dir, exist_ok=True)
            
            # 2. 检查是否有现有的active.md文件
            existing_file_path = os.path.join(target_dir, "active.md")
            if os.path.exists(existing_file_path):
                logger.info(f"Found existing active.md for {directory_path}")
            else:
                existing_file_path = None
                logger.info(f"No existing active.md found for {directory_path}")
            
            # 过滤出当前目录相关的文件变更
            directory_changes = {}
            if file_changes:
                # 获取当前目录下的所有文件路径
                dir_files = []
                for file_info in context.get('changed_files', []):
                    dir_files.append(file_info['path'])
                for file_info in context.get('current_files', []):
                    dir_files.append(file_info['path'])
                
                # 从file_changes中获取当前目录文件的变更
                for file_path, change_info in file_changes.items():
                    if file_path in dir_files:
                        directory_changes[file_path] = change_info
                
                logger.info(f"Found {len(directory_changes)} relevant file changes for {directory_path}")
            
            # 3. 生成活动文件内容，传递现有文件路径（如果有）和文件变更信息
            markdown_content = self.active_package.generate_active_file(
                context, 
                query,
                existing_file_path=existing_file_path,
                file_changes=directory_changes
            )
            
            # 4. 写入文件
            active_md_path = os.path.join(target_dir, "active.md")
            with open(active_md_path, "w", encoding="utf-8") as f:
                f.write(markdown_content)
                
            logger.info(f"Created/Updated active.md for {directory_path}")
        except Exception as e:
            logger.error(f"Error processing directory {context.get('directory_path', 'unknown')}: {e}")
            raise
    
    def get_task_status(self, task_id: str) -> Dict[str, Any]:
        """
        获取任务状态
        
        Args:
            task_id: 任务ID
            
        Returns:
            Dict: 任务状态信息
        """
        if task_id not in self.tasks:
            return {'status': 'not_found', 'task_id': task_id}
        return self.tasks[task_id]
    
    def get_all_tasks(self) -> List[Dict[str, Any]]:
        """
        获取所有任务状态
        
        Returns:
            List[Dict]: 所有任务的状态信息
        """
        return [{'task_id': tid, **task} for tid, task in self.tasks.items()]
    
    def get_running_tasks(self) -> List[Dict[str, Any]]:
        """
        获取所有正在运行的任务
        
        Returns:
            List[Dict]: 所有正在运行的任务的状态信息
        """
        return [{'task_id': tid, **task} for tid, task in self.tasks.items() 
                if task['status'] == 'running']
    
    def load_active_contexts_for_files(self, file_paths: List[str]) -> FileContextsResult:
        """
        根据文件路径列表，找到并加载对应的活动上下文文件
        
        Args:
            file_paths: 文件路径列表
            
        Returns:
            FileContextsResult: 包含活动上下文信息的结构化结果
        """
        try:
            result = FileContextsResult()
            
            # 记录未找到对应活动上下文的文件
            found_files: Set[str] = set()
            
            # 1. 获取文件所在的唯一目录列表
            directories = set()
            for file_path in file_paths:
                # 获取文件所在的目录
                dir_path = os.path.dirname(file_path)
                if os.path.exists(dir_path):
                    directories.add(dir_path)
            
            # 2. 查找每个目录的活动上下文文件
            for dir_path in directories:
                # 获取活动上下文目录路径
                active_context_dir = self._get_active_context_path(dir_path)
                active_md_path = os.path.join(active_context_dir, "active.md")
                
                # 检查active.md文件是否存在
                if os.path.exists(active_md_path):
                    try:
                        # 读取文件内容
                        with open(active_md_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        # 解析文件内容
                        sections_dict = self._parse_active_md_content(content)
                        sections = ActiveFileSections(**sections_dict)
                        
                        # 找到相关的文件
                        related_files = [f for f in file_paths if dir_path in f]
                        
                        # 记录找到了对应活动上下文的文件
                        found_files.update(related_files)
                        
                        # 创建活动文件上下文
                        active_context = ActiveFileContext(
                            directory_path=dir_path,
                            active_md_path=active_md_path,
                            content=content,
                            sections=sections,
                            files=related_files
                        )
                        
                        # 添加到结果
                        result.contexts[dir_path] = active_context
                        
                        logger.info(f"已加载目录 {dir_path} 的活动上下文文件")
                    except Exception as e:
                        logger.error(f"读取活动上下文文件 {active_md_path} 时出错: {e}")
            
            # 3. 记录未找到对应活动上下文的文件
            result.not_found_files = [f for f in file_paths if f not in found_files]
            
            return result
            
        except Exception as e:
            logger.error(f"加载活动上下文失败: {e}")
            return FileContextsResult(not_found_files=file_paths)
    
    def _parse_active_md_content(self, content: str) -> Dict[str, str]:
        """
        解析活动上下文文件内容，提取各个部分
        
        Args:
            content: 活动上下文文件内容
            
        Returns:
            Dict[str, str]: 包含标题、当前变更和文档部分的字典
        """
        try:
            result = {
                'header': '',
                'current_change': '',
                'document': ''
            }
            
            # 提取标题部分（到第一个二级标题之前）
            header_match = re.search(r'^(.*?)(?=\n## )', content, re.DOTALL)
            if header_match:
                result['header'] = header_match.group(1).strip()
            
            # 提取当前变更部分
            current_change_match = re.search(r'## 当前变更\s*\n(.*?)(?=\n## |$)', content, re.DOTALL)
            if current_change_match:
                result['current_change'] = current_change_match.group(1).strip()
            
            # 提取文档部分
            document_match = re.search(r'## 文档\s*\n(.*?)(?=\n## |$)', content, re.DOTALL)
            if document_match:
                result['document'] = document_match.group(1).strip()
            
            return result
        except Exception as e:
            logger.error(f"解析活动上下文文件内容时出错: {e}")
            return {'header': '', 'current_change': '', 'document': ''}
    
    def _get_active_context_path(self, directory_path: str) -> str:
        """
        获取活动上下文中对应的目录路径
        
        Args:
            directory_path: 原始目录路径
            
        Returns:
            str: 活动上下文中对应的目录路径
        """
        try:
            relative_path = os.path.relpath(directory_path, self.args.source_dir)
            return os.path.join(self.args.source_dir, ".auto-coder", "active-context", relative_path)
        except ValueError:
            # 处理可能的跨设备相对路径错误
            # 使用绝对路径的最后部分
            parts = os.path.normpath(directory_path).split(os.sep)
            # 取最后两级目录，如果存在
            if len(parts) >= 2:
                relative_path = os.path.join(parts[-2], parts[-1])
            else:
                relative_path = parts[-1]
            return os.path.join(self.args.source_dir, ".auto-coder", "active-context", relative_path)
        