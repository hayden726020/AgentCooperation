import os
import re
import time
from pathlib import Path
from typing import Dict, Any, Optional
from openai import AsyncOpenAI
from agents.base import BaseAgent, AgentResult
from config.settings import ModelPricing, BudgetConfig


class FlashApiAgent(BaseAgent):
    """
    主力 Flash 研发 Agent（GLM-5.3-Flash 或 DeepSeek-Flash，互斥单模型）
    采用原生 OpenAI 兼容协议异步调用，实现精确 Token 与 ¥ 成本计量。
    """

    def __init__(
        self,
        model_key: str,  # "glm" 或 "deepseek"
        pricing: ModelPricing,
        budget_cfg: BudgetConfig,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        mock_mode: bool = False
    ):
        super().__init__(role_name="PRIMARY_DEVELOPER", model_name=pricing.model_id)
        self.model_key = model_key
        self.pricing = pricing
        self.budget_cfg = budget_cfg
        self.api_key = api_key or os.getenv(f"{model_key.upper()}_API_KEY") or os.getenv("FLASH_API_KEY")
        self.base_url = base_url or pricing.base_url or os.getenv("FLASH_BASE_URL")
        self.mock_mode = mock_mode

        if not self.api_key:
            # 若无 API key，则自动启用优雅 Mock 模式，确保测试与离线演示可用
            self.mock_mode = True

        if not self.mock_mode:
            self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        else:
            self.client = None

    def calculate_cost_cny(self, prompt_tokens: int, completion_tokens: int) -> float:
        cost_usd = (
            (prompt_tokens / 1_000_000.0) * self.pricing.input_price_per_m_usd
            + (completion_tokens / 1_000_000.0) * self.pricing.output_price_per_m_usd
        )
        return round(cost_usd * self.budget_cfg.usd_to_cny_rate, 5)

    def extract_and_apply_files(self, content: str, target_dir: Path) -> list[str]:
        """
        从模型输出中提取生成/修改的代码文件并写入指定目录。
        支持格式：
        ```lang:path/to/file.ext 或 ```path/to/file.ext
        代码...
        ```
        以及带有 ### File: path/to/file.ext 标记的代码块。
        """
        written_files = []
        target_dir.mkdir(parents=True, exist_ok=True)

        # 模式 1: ```python:filepath 或 ```filepath
        code_block_pattern = re.compile(r"```(?:[a-zA-Z0-9_\-]+:)?([a-zA-Z0-9_\-\.\/\\\:]+)\s*\n(.*?)```", re.DOTALL)
        # 模式 2: ### File: filepath \n ```lang\n ... \n```
        file_header_pattern = re.compile(r"(?:###|#|File:)\s*([a-zA-Z0-9_\-\.\/\\\:]+)\s*\n\s*```[a-zA-Z0-9_\-]*\s*\n(.*?)```", re.DOTALL)

        matches = list(file_header_pattern.finditer(content))
        if not matches:
            matches = list(code_block_pattern.finditer(content))

        for m in matches:
            file_name = m.group(1).strip()
            # 过滤纯语言标识（如 python, json 等不含扩展名且无路径的）
            if "." not in file_name or " " in file_name:
                continue
            
            code_body = m.group(2)
            clean_path = file_name.replace(":", "").strip()
            dest_file = target_dir / clean_path
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_file, "w", encoding="utf-8") as f:
                f.write(code_body)
            written_files.append(str(dest_file))

        return written_files

    async def execute(self, prompt: str, context: Optional[Dict[str, Any]] = None) -> AgentResult:
        start_time = time.time()
        context = context or {}
        worktree_path = context.get("worktree_path")
        system_instruction = context.get(
            "system_instruction",
            "You are an expert full-stack developer. Write complete, high-quality, production code and tests. "
            "When creating or updating files, wrap each file with markdown code block specifying the relative filename: "
            "```python:filename.py\n...code...\n```"
        )

        if self.mock_mode:
            # 离线或无 API Key 时的行为
            duration = 0.5
            p_tokens = 500
            c_tokens = 400
            cost = self.calculate_cost_cny(p_tokens, c_tokens)
            
            # 针对测试提供模拟代码
            mock_content = (
                f"### [Mock {self.model_name}] Implementation\n\n"
                "I have implemented the requested functionality with unit tests.\n\n"
                "```python:solution.py\n"
                "def solve():\n"
                "    return True\n"
                "```\n\n"
                "```python:test_solution.py\n"
                "from solution import solve\n\n"
                "def test_solution():\n"
                "    assert solve() is True\n"
                "```\n"
            )

            if worktree_path:
                self.extract_and_apply_files(mock_content, Path(worktree_path))

            return AgentResult(
                success=True,
                content=mock_content,
                prompt_tokens=p_tokens,
                completion_tokens=c_tokens,
                cost_cny=cost,
                duration_s=duration,
                metadata={"mock": True, "files_written": ["solution.py", "test_solution.py"]}
            )

        # 真实 API 调用
        try:
            response = await self.client.chat.completions.create(
                model=self.pricing.model_id,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2
            )

            duration = time.time() - start_time
            content = response.choices[0].message.content or ""
            usage = response.usage
            prompt_tokens = usage.prompt_tokens if usage else 0
            completion_tokens = usage.completion_tokens if usage else 0
            cost = self.calculate_cost_cny(prompt_tokens, completion_tokens)

            written = []
            if worktree_path:
                written = self.extract_and_apply_files(content, Path(worktree_path))

            return AgentResult(
                success=True,
                content=content,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_cny=cost,
                duration_s=duration,
                metadata={"files_written": written}
            )

        except Exception as e:
            duration = time.time() - start_time
            return AgentResult(
                success=False,
                content="",
                duration_s=duration,
                error=str(e)
            )
