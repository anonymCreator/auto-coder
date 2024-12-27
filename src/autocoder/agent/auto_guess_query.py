from typing import List, Dict, Any, Tuple, Optional
import os
import yaml
from loguru import logger
import byzerllm
import pydantic
import git


class NextQuery(pydantic.BaseModel):
    """下一个开发任务的描述和相关信息"""
    query: str = pydantic.Field(description="任务需求描述")
    urls: List[str] = pydantic.Field(description="预测可能需要修改的文件路径列表")
    priority: int = pydantic.Field(description="任务优先级，1-5，5为最高优先级")
    reason: str = pydantic.Field(description="为什么需要这个任务，以及为什么需要修改这些文件")
    dependency_queries: List[str] = pydantic.Field(description="依赖的历史任务列表", default_factory=list)


def load_yaml_config(yaml_file: str) -> Dict:
    """加载YAML配置文件"""
    try:
        with open(yaml_file, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Error loading yaml file {yaml_file}: {str(e)}")
        return {}


class AutoGuessQuery:
    def __init__(self, llm: byzerllm.ByzerLLM,
                 project_dir: str,
                 skip_diff: bool = False,
                 file_size_limit: int = 100):
        """
        初始化 AutoGuessQuery

        Args:
            llm: ByzerLLM 实例，用于生成下一步任务预测
            project_dir: 项目根目录
            skip_diff: 是否跳过获取 diff 信息
            file_size_limit: 最多分析多少历史任务
        """
        self.project_dir = project_dir
        self.actions_dir = os.path.join(project_dir, "actions")
        self.llm = llm
        self.file_size_limit = file_size_limit
        self.skip_diff = skip_diff

    @byzerllm.prompt()
    def guess_next_query(self, querie_with_urls: List[Tuple[str, List[str], str]]) -> str:
        """
        根据历史开发任务，预测下一步可能的开发任务。

        输入数据格式：
        querie_with_urls 包含多个历史任务信息，每个任务由以下部分组成：
        1. query: 任务需求描述
        2. urls: 修改的文件路径列表
        3. diff: Git diff信息，展示具体的代码修改

        示例数据：
        <queries>
        {% for query,urls,diff in querie_with_urls %}
        ## {{ query }}        

        修改的文件:
        {% for url in urls %}
        - {{ url }}
        {% endfor %}
        {% if diff %}

        代码变更:
        ```diff
        {{ diff }}
        ```
        {% endif %}        
        {% endfor %}
        </queries>

        分析要求：
        1. 分析历史任务的模式和规律
           - 功能演进路径：项目功能是如何逐步完善的
           - 代码变更模式：相似功能通常涉及哪些文件
           - 依赖关系：新功能和已有功能的关联
           
        2. 预测下一步任务时考虑：
           - 完整性：现有功能是否有待完善的地方
           - 扩展性：是否需要支持新的场景
           - 健壮性：是否需要增加容错和异常处理
           - 性能：是否有性能优化空间

        返回格式说明：
        返回 NextQuery 对象对应的 JSON 字符串，包含：
        1. query: 下一步任务的具体描述
        2. urls: 预计需要修改的文件列表
        3. priority: 优先级(1-5)
        4. reason: 为什么建议这个任务
        5. dependency_queries: 相关的历史任务列表

        注意：
        1. 预测的任务应该具体且可执行，而不是抽象的目标
        2. 文件路径预测应该基于已有文件的实际路径
        3. reason应该解释为什么这个任务重要，以及为什么需要修改这些文件
        4. priority的指定需要考虑任务的紧迫性和重要性
        """
        pass

    def parse_history_tasks(self) -> List[Dict]:
        """
        解析历史任务信息

        Returns:
            List[Dict]: 每个字典包含一个历史任务的信息
        """
        # 获取所有YAML文件
        action_files = [
            f for f in os.listdir(self.actions_dir)
            if f[:3].isdigit() and "_" in f and f.endswith('.yml')
        ]

        # 按序号排序
        def get_seq(name):
            return int(name.split("_")[0])

        # 获取最新的action文件列表
        action_files = sorted(action_files, key=get_seq)
        action_files.reverse()

        action_files = action_files[:self.file_size_limit]

        querie_with_urls_and_diffs = []
        repo = git.Repo(self.project_dir)

        # 收集所有query、urls和对应的commit diff
        for yaml_file in action_files:
            yaml_path = os.path.join(self.actions_dir, yaml_file)
            config = load_yaml_config(yaml_path)

            if not config:
                continue

            query = config.get('query', '')
            urls = config.get('urls', [])

            if query and urls:
                commit_diff = ""
                if not self.skip_diff:
                    # 计算文件的MD5用于匹配commit
                    import hashlib
                    file_md5 = hashlib.md5(open(yaml_path, 'rb').read()).hexdigest()
                    response_id = f"auto_coder_{yaml_file}_{file_md5}"
                    # 查找对应的commit                   
                    try:
                        for commit in repo.iter_commits():
                            if response_id in commit.message:
                                if commit.parents:
                                    parent = commit.parents[0]
                                    commit_diff = repo.git.diff(
                                        parent.hexsha, commit.hexsha)
                                else:
                                    commit_diff = repo.git.show(commit.hexsha)
                                break
                    except git.exc.GitCommandError as e:
                        logger.error(f"Git命令执行错误: {str(e)}")
                    except Exception as e:
                        logger.error(f"获取commit diff时出错: {str(e)}")

                querie_with_urls_and_diffs.append((query, urls, commit_diff))

        return querie_with_urls_and_diffs

    def predict_next_task(self) -> Optional[NextQuery]:
        """
        预测下一步开发任务

        Returns:
            NextQuery: 预测的下一个任务信息，如果预测失败则返回None
        """
        history_tasks = self.parse_history_tasks()
        
        if not history_tasks:
            logger.warning("No history tasks found")
            return None

        try:
            result = self.guess_next_query.with_llm(self.llm).run(
                querie_with_urls=history_tasks
            )
            import json
            next_query = NextQuery.parse_raw(result)
            return next_query
        except Exception as e:
            import traceback
            traceback.print_exc()
            logger.error(f"Error predicting next task: {str(e)}")
            return None