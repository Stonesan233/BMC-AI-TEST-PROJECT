# -*- coding: utf-8 -*-
"""
BMC CLI 命令文档 DOCX 解析器 (v2 重构版)

适用文档: Atlas 系列 iBMC 用户指南 (.docx 格式)

文档特征:
  - 所有段落均为 "Normal" 样式，标题仅通过字号区分
  - 字号层级: 72pt (章) > 18pt (子节) > 16pt (命令标题) > 13pt (标签) > 10.5pt (正文) > 9.5pt (说明)
  - **合并段落** (65 处): 16pt 标题 + 13pt 标签 + 10.5pt 正文在同一段落 (run 级字号区分)
  - **完全合并段落** (15 处): 标题 + 命令功能 + 命令格式 + 参数说明 + 使用指南 + 使用实例
    全部在一个段落中，仅通过 run 级 13pt 标签边界分隔
  - 标签段 (13pt): "命令功能", "命令格式", "参数说明", "使用指南", "使用实例"
  - 命令示例存储在表格中 (1r x 1c, 含 "iBMC:/->" 提示符)
  - 参数说明存储在表格中 (Nr x 2~3c, 含 "参数" 列头)
  - 约 46% 噪声表格 (页眉: "Atlas 900...用户指南", 页脚: "文档版本 04")

v2 重构改进:
  - Run 级解析: 精确处理 65 种合并段落, 15 种完全合并段落
  - 标签状态机: FUNCTION -> SYNTAX -> PARAMETERS -> USAGE -> EXAMPLE
  - 精确 command_name/subcommand 提取: 支持复合名称 "service -d state"
  - interaction_steps: 从示例表格提取交互提示序列
  - URI-first 风格 chunk text: 命令名前置, 结构化格式
  - 丰富同义词映射: 100+ 操作关键词覆盖

依赖: python-docx>=0.8.11
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from src.rag.parsers.base_parser import BaseParser

logger = logging.getLogger("rag.parser.docx_cli")

# ============================================================================
# 常量
# ============================================================================

FONT_SIZE_CHAPTER = 72.0
FONT_SIZE_SECTION = 18.0
FONT_SIZE_COMMAND = 16.0
FONT_SIZE_LABEL = 12.5
FONT_SIZE_BODY = 10.5

# ---------------------------------------------------------------------------
# 噪声过滤
# ---------------------------------------------------------------------------
_NOISE_TABLE_KEYWORDS = [
    "文档版本",
    "iBMC用户指南",
    "用户指南",
    "华为技术有限公司",
    "版权所有",
    "Atlas 900",
    "iBMC\n用户指南",
]
_TOC_DOT_PATTERN = re.compile(r"\.{4,}")

# ---------------------------------------------------------------------------
# CLI 章节边界检测
# ---------------------------------------------------------------------------
_CLI_CHAPTER_PATTERN = re.compile(r"^7\s*CLI", re.IGNORECASE)
_CLI_END_CHAPTER_PATTERN = re.compile(r"^(8|9|10|11)\s")

# ---------------------------------------------------------------------------
# 需要跳过的非命令子节
# ---------------------------------------------------------------------------
_SKIP_TITLES = frozenset({
    "7.1 CLI 说明",
    "7.2 登录CLI",
    "7.1.1 格式说明",
    "7.1.2 帮助",
    "7.2.1 通过管理网口登录CLI",
    "7.2.2 通过串口登录CLI",
})

# ---------------------------------------------------------------------------
# 标签 -> 状态映射 (状态机)
# ---------------------------------------------------------------------------
_LABEL_STATE_MAP = [
    ("命令功能", "FUNCTION"),
    ("命令格式", "SYNTAX"),
    ("参数说明", "PARAMETERS"),
    ("使用指南", "USAGE"),
    ("使用实例", "EXAMPLE"),
    ("前提条件", "PREREQUISITE"),
    ("操作步骤", "STEPS"),
    ("后续处理", "USAGE"),
]

# ---------------------------------------------------------------------------
# 命令提取正则
# ---------------------------------------------------------------------------
# 主命令: ipmcget, ipmcset, ipmitool, help, who, ping, ...
_CLI_MAIN_COMMANDS = (
    r"ipmcset", r"ipmcget", r"ipmitool",
    r"help", r"who", r"clearcmos", r"mcinfo",
    r"ping\d*", r"ifconfig", r"top", r"free", r"df",
    r"ps", r"kill", r"cat", r"cd", r"ls", r"rm",
    r"tar", r"unzip", r"reboot", r"poweroff", r"exit",
    r"logout", r"notimeout", r"netstat",
)
_CLI_CMD_RE = re.compile(
    r"\b(" + "|".join(_CLI_MAIN_COMMANDS) + r")\b",
    re.IGNORECASE,
)
# 从语法行提取: ipmcset -d xxx 或 ipmcset -t xxx -d xxx [-v ...]
_SYNTAX_FULL_RE = re.compile(
    r"(ipmc(?:set|get)|ipmitool)"
    r"(\s+-t\s+\S+)?"        # optional -t target
    r"\s+-d\s+(\S+)"         # -d dataitem (capture)
    r"(?:\s+-v\s+.+)?$",     # optional -v value
    re.IGNORECASE,
)
# 交互式提示提取
_INTERACTION_PROMPT_RE = re.compile(
    r"(Input your password[:\s]*|"
    r"Password[:\s]*|"
    r"Confirm password[:\s]*|"
    r"New password[:\s]*|"
    r"Continue\?\s*\[[Yy]/[Nn]\][:：]?|"
    r"请输入[^：:\n]*[：:]?\s*|"
    r"请确认[^：:\n]*[：:]?\s*)",
    re.IGNORECASE,
)
# iBMC 提示行检测
_IBMC_PROMPT_RE = re.compile(r"iBMC:/?->")

# ---------------------------------------------------------------------------
# 交互式检测
# ---------------------------------------------------------------------------
_INTERACTIVE_EXAMPLE_KEYWORDS = [
    "Input your password",
    "Password:",
    "Confirm password:",
    "New password:",
    "[Y/N]",
    "[y/N]",
    "[Y/n]",
    "Continue?",
]
_INTERACTIVE_USAGE_KEYWORDS = [
    "需要输入当前管理员",
    "需要输入密码",
    "需要输入用户",
    "需要确认",
    "输入新密码",
    "操作过程中需要输入",
    "交互式",
]

# ---------------------------------------------------------------------------
# 同义词映射 (关键词 -> 同义词字符串)
# ---------------------------------------------------------------------------
_SYNONYM_MAP: Dict[str, str] = {
    # 用户管理
    "添加用户": "adduser 创建用户 新增用户 new user add user 创建账号 新增账号",
    "删除用户": "deluser 删除账号 移除用户 remove user delete user 删除账号",
    "修改密码": "password 改密码 更改密码 change password 修改口令 设置密码",
    "设置权限": "privilege 权限 角色 permission role 设置角色 用户权限",
    "查询用户": "userlist list 用户列表 查看用户 show user list users 获取用户列表",
    "锁定用户": "lock 禁用用户 block user disable user 冻结用户",
    "解锁用户": "unlock 解除锁定 enable user unblock user 恢复用户",
    "启用状态": "state 启用 禁用 enable disable 激活 停用",
    "密码复杂": "passwordcomplexity 密码检查 密码策略 password policy 密码强度",
    "弱口令": "weakpwddic 弱密码 weak password 密码字典",
    "紧急用户": "emergencyuser 紧急登录 emergency 紧急账号",
    "SSH公钥": "addpublickey SSH密钥 SSH key 公钥 public key",
    # 网络管理
    "设置IP": "ipaddr 设置IP地址 set ip address 配置网络 修改IP IPv4地址",
    "查询IP": "ipinfo IP信息 show ip 查看IP IP地址信息",
    "备份IP": "backupipaddr 备份IP地址 备用IP backup ip",
    "IP模式": "ipmode 地址模式 dhcp static 静态 动态",
    "网关": "gateway 网关地址 默认网关 default gateway",
    "IPv6": "ipaddr6 IPv6地址 ipv6 address 前缀 prefix",
    "网口模式": "netmode 网络模式 network mode 端口模式",
    "激活端口": "activeport 激活端口 active port",
    "网络协议": "ipversion 协议版本 IP版本",
    # 系统
    "重启": "reboot reset 重置 重新启动 restart 重启BMC 重启iBMC",
    "版本": "version 版本信息 固件版本 firmware version 查询版本",
    "恢复出厂": "restore 出厂设置 factory reset 恢复默认 恢复出厂",
    "截屏": "printscreen 截屏 screenshot 屏幕截图",
    "SEL": "sel 系统事件日志 event log 系统日志 清除日志",
    "启动设备": "bootdevice 启动设备 启动顺序 boot order boot device",
    "电源状态": "powerstate 上下电 上电 下电 power control",
    "清除CMOS": "clearcmos 清CMOS 清除BIOS clear bios",
    # 服务
    "服务状态": "service 服务 enable disable 启用服务 禁用服务",
    "端口": "port 端口号 service port 服务端口",
    "SSL证书": "certificate SSL cert 证书导入 证书信息",
    "配置文件": "config 导入配置 import config export 导出配置",
    "虚拟光驱": "vmm 虚拟媒体 virtual media 挂载 mount",
    # SNMP/Trap
    "trap": "trap SNMP trap 告警上报 SNMP告警",
    "syslog": "syslog 日志服务器 系统日志上报 log server",
    "VNC": "vnc 远程桌面 VNC服务 远程控制",
    # NTP
    "NTP": "ntp 时间同步 time sync NTP服务器 时间服务器",
    # 传感器
    "传感器": "sensor 传感器列表 查看传感器 sensor list",
    # 指示灯
    "指示灯": "locator LED 指示灯 定位灯",
    # 风扇
    "风扇": "fan 风扇策略 fan policy 散热 cooling",
    # 电源
    "电源": "power 电源状态 查看电源 power status",
    # SOL
    "SOL": "sol 串口 串行会话 serial session",
    # LSW
    "LSW": "lsw 交换芯片 switch reset 复位",
    # 安全
    "安全信息": "securitybanner 登录安全 安全横幅 login banner security banner",
    "主密钥": "masterkey 主密钥 master key 密钥更新",
    "安全加固": "securityenhance 安全加固 security enhance",
    # RAID
    "逻辑盘": "ldconfig 逻辑盘 logical drive RAID配置",
    "RAID": "ctrlconfig RAID控制器 RAID controller 物理盘 pdconfig",
    # 固件
    "固件升级": "upgrade 固件更新 firmware update 刷新固件",
    "固件版本": "fwversion 固件版本号 firmware version",
    # 登录
    "登录规则": "loginrule 登录限制 login rule 访问控制",
    "登录接口": "logininterface 登录方式 login interface",
    "会话": "session 会话管理 session 会话超时 timeout",
    # DNS
    "DNS": "dns 域名解析 domain name server",
    # LDAP
    "LDAP": "ldap 目录服务 directory service 域认证",
    "证书": "certificate 证书管理 cert trust 信任证书",
}

# ---------------------------------------------------------------------------
# 命令标题解析正则
# ---------------------------------------------------------------------------
# 格式: "7.3.2 设置iBMC 管理网口的IPv4 信息（ipaddr）"
_TITLE_RE = re.compile(
    r"^(?P<num>\d+[\.\d]*)\s+"
    r"(?P<cn>[^(\uff08（]+?)"
    r"(?:[(\uff08（](?P<en>[^)\uff09）]+)[)\uff09）])?"
    r"\s*$"
)
# 复合英文名: "service -d state", "ntp -d extraserver"
_COMPOUND_EN_RE = re.compile(
    r"^([a-zA-Z][a-zA-Z0-9]*)\s+-d\s+([a-zA-Z][a-zA-Z0-9]*)$"
)


# ============================================================================
# 命令收集器 (CommandCollector)
# ============================================================================

@dataclass
class CommandCollector:
    """
    单个 CLI 命令的收集器 (v2).

    状态机:
      IDLE -> FUNCTION -> SYNTAX -> PARAMETERS -> USAGE -> EXAMPLE -> IDLE

    每次 flush 或 reset 后回到 IDLE.
    """
    title: str = ""
    chinese_name: str = ""
    english_name: str = ""
    section_number: str = ""

    # 收集内容 (按状态分类)
    function_lines: List[str] = field(default_factory=list)
    syntax_lines: List[str] = field(default_factory=list)
    parameter_tables: List[str] = field(default_factory=list)
    usage_lines: List[str] = field(default_factory=list)
    example_tables: List[str] = field(default_factory=list)

    # 状态
    state: str = "IDLE"  # FUNCTION / SYNTAX / PARAMETERS / USAGE / EXAMPLE / IDLE
    command_name: str = ""
    subcommand: str = ""
    full_command: str = ""
    is_interactive: bool = False
    interaction_steps: List[str] = field(default_factory=list)

    @property
    def is_collecting(self) -> bool:
        return len(self.title) > 0

    def set_state(self, new_state: str) -> None:
        """切换状态."""
        self.state = new_state

    def add_text(self, text: str) -> None:
        """根据当前状态将文本归入对应部分."""
        st = self.state
        if st == "FUNCTION":
            self.function_lines.append(text)
        elif st == "SYNTAX":
            self.syntax_lines.append(text)
        elif st == "PARAMETERS":
            self.function_lines.append(text)  # 参数说明的文字补充
        elif st == "USAGE":
            self.usage_lines.append(text)
        elif st == "EXAMPLE":
            self.usage_lines.append(text)  # 示例的文字说明
        elif st == "PREREQUISITE":
            self.usage_lines.append(text)
        else:
            self.usage_lines.append(text)

    def add_table(self, table_text: str) -> None:
        """将表格内容归入当前命令."""
        st = self.state
        if st == "PARAMETERS":
            self.parameter_tables.append(table_text)
        elif st == "EXAMPLE":
            self.example_tables.append(table_text)
            self._check_interactive_table(table_text)
        elif st == "SYNTAX":
            self.syntax_lines.append(table_text)
        else:
            # 未知状态的表格归入示例 (多数是示例输出)
            self.example_tables.append(table_text)
            self._check_interactive_table(table_text)

    def _check_interactive_table(self, text: str) -> None:
        """从示例表格中检测交互式特征和交互步骤."""
        text_lower = text.lower()
        for kw in _INTERACTIVE_EXAMPLE_KEYWORDS:
            if kw.lower() in text_lower:
                self.is_interactive = True
                break
        # 提取交互步骤
        for line in text.split("\n"):
            line = line.strip()
            m = _INTERACTION_PROMPT_RE.match(line)
            if m:
                prompt = m.group(0).strip().rstrip(":：")
                if prompt and prompt not in self.interaction_steps:
                    self.interaction_steps.append(prompt)

    def check_interactive_usage(self, text: str) -> None:
        """从使用指南文本检测交互式特征."""
        if self.is_interactive:
            return
        for kw in _INTERACTIVE_USAGE_KEYWORDS:
            if kw in text:
                self.is_interactive = True
                break

    def update_command_info(self, text: str) -> None:
        """从文本中提取命令名和子命令."""
        # 先尝试从语法行精确提取
        m = _SYNTAX_FULL_RE.search(text)
        if m:
            self.command_name = m.group(1).lower()
            self.subcommand = f"-d {m.group(3).lower()}"
            self.full_command = text.strip()
            return
        # 兜底: 从文本中提取主命令
        if not self.command_name:
            m = _CLI_CMD_RE.search(text)
            if m:
                self.command_name = m.group(1).lower()
        # 兜底: 从 -d xxx 提取子命令
        if not self.subcommand:
            m = re.search(r"-d\s+(\S+)", text)
            if m:
                self.subcommand = f"-d {m.group(1).lower()}"

    def reset(self) -> None:
        """重置收集器."""
        self.title = ""
        self.chinese_name = ""
        self.english_name = ""
        self.section_number = ""
        self.function_lines = []
        self.syntax_lines = []
        self.parameter_tables = []
        self.usage_lines = []
        self.example_tables = []
        self.state = "IDLE"
        self.command_name = ""
        self.subcommand = ""
        self.full_command = ""
        self.is_interactive = False
        self.interaction_steps = []


# ============================================================================
# Run 级解析辅助
# ============================================================================

def _looks_like_command_title(text: str) -> bool:
    """
    判断 16pt 段落是否为真正的命令标题.

    真正的命令标题特征:
      - 以数字编号开头 (如 "7.3.2 设置...")

    非标题的 16pt 文本 (应降级为 body):
      - 以列表符号开头: ▪, ●, –, ※, ★, ✓
      - 以小写字母或短单词开头 (如 "a ～ z")
    """
    # 以数字编号开头 -> 一定是命令标题
    if re.match(r"^\d+\.\d", text):
        return True
    # 以列表符号开头 -> 不是标题
    if re.match(r"^[▪●–※★✓·→►]", text):
        return False
    # 以单个标点或空格开头 -> 不是标题
    if text[0] in " \t–-*":
        return False
    # 其他情况: 保守判定为标题
    return True

@dataclass
class RunSegment:
    """一个 run 的字号和文本."""
    pt: float
    text: str


def _extract_run_segments(para: Paragraph) -> List[RunSegment]:
    """提取段落中所有有效 run 的字号和文本."""
    segments: List[RunSegment] = []
    for run in para.runs:
        text = run.text
        if not text:
            continue
        pt = run.font.size.pt if run.font.size else FONT_SIZE_BODY
        segments.append(RunSegment(pt=pt, text=text))
    return segments


def _is_merged_paragraph(segments: List[RunSegment]) -> bool:
    """检测是否为合并段落 (>=16pt 和 <16pt 的 run 共存)."""
    has_cmd_size = False
    has_body_size = False
    for seg in segments:
        if not seg.text.strip():
            continue
        if seg.pt >= FONT_SIZE_COMMAND:
            has_cmd_size = True
        elif seg.pt < FONT_SIZE_COMMAND:
            has_body_size = True
    return has_cmd_size and has_body_size


def _parse_merged_runs(
    segments: List[RunSegment],
) -> Tuple[str, List[Tuple[str, str]]]:
    """
    解析合并段落中的 run, 提取标题文本和后续的 (标签状态, 文本) 对.

    逻辑:
      1. 第一个 >=16pt 的 run 组合为标题部分
      2. 后续的 run 按 13pt 标签检测切换状态
      3. 10.5pt 正文归入当前状态

    Returns:
        (title_text, [(state, text), ...])
    """
    title_parts: List[str] = []
    extra_parts: List[Tuple[str, str]] = []
    current_state = "TITLE"
    buffer: List[str] = []

    def flush_buffer():
        nonlocal buffer
        if buffer:
            joined = "".join(buffer).strip()
            if joined and current_state != "TITLE":
                extra_parts.append((current_state, joined))
            buffer = []

    for seg in segments:
        stripped = seg.text.strip()
        if not stripped:
            # 保留换行 (标签边界标识)
            if "\n" in seg.text and current_state == "TITLE" and title_parts:
                pass  # 标题内换行, 忽略
            elif "\n" in seg.text and current_state != "TITLE":
                flush_buffer()
            continue

        if seg.pt >= FONT_SIZE_COMMAND and current_state == "TITLE":
            title_parts.append(seg.text)
        else:
            # 离开标题区域
            if current_state == "TITLE" and title_parts:
                current_state = "AFTER_TITLE"
                flush_buffer()

            # 检测标签切换
            new_state = _detect_label_state(stripped)
            if new_state:
                flush_buffer()
                current_state = new_state
                continue

            # 正文内容
            if current_state == "AFTER_TITLE":
                # 标题后紧跟非标签正文, 视为功能描述
                current_state = "FUNCTION"

            if current_state != "TITLE":
                buffer.append(seg.text)

    flush_buffer()
    title_text = "".join(title_parts).strip()
    return title_text, extra_parts


def _detect_label_state(text: str) -> Optional[str]:
    """检测文本是否为标签, 返回对应状态或 None."""
    for label_text, state in _LABEL_STATE_MAP:
        if label_text in text:
            return state
    return None


# ============================================================================
# DocxCLIParser (v2)
# ============================================================================

class DocxCLIParser(BaseParser):
    """
    BMC CLI 命令文档 DOCX 解析器 (v2 重构版).

    核心改进:
      - Run 级解析: 精确处理 65 种合并段落 (16pt+13pt+10.5pt 混合)
      - 标签状态机: FUNCTION -> SYNTAX -> PARAMETERS -> USAGE -> EXAMPLE
      - 复合命令名支持: "service -d state", "ntp -d extraserver"
      - interaction_steps: 从示例表格提取交互提示序列
      - URI-first 风格 chunk text: command_name 前置, 结构化格式
      - 丰富同义词映射: 100+ 操作关键词覆盖
    """

    async def parse(self, file_path: str) -> List[Dict[str, Any]]:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        if path.suffix.lower() != ".docx":
            raise ValueError(f"仅支持 .docx 文件，收到: {path.suffix}")

        logger.info(f"开始解析: {path.name}")
        doc = Document(str(path))
        file_name = path.name

        chunks: List[Dict[str, Any]] = []
        chunk_counter: int = 0
        current_section: str = ""
        in_cli: bool = False
        collector = CommandCollector()

        for elem_type, elem in self._iter_body_elements(doc):

            if elem_type == "paragraph":
                para = Paragraph(elem, doc)
                segments = _extract_run_segments(para)
                if not segments:
                    continue

                level, text = self._classify_by_runs(segments)

                if level == "empty":
                    continue

                # ---- 章标题 (72pt) ----
                if level == "chapter":
                    if _CLI_CHAPTER_PATTERN.search(text.replace(" ", "")):
                        in_cli = True
                        chunk_counter += 1
                        chunks.append(self._build_chunk(
                            text=f"章节: {text}",
                            file_name=file_name,
                            chunk_id=f"chapter_{chunk_counter:05d}",
                            chunk_type="chapter",
                            section=text,
                            doc_type="cli",
                        ))
                    elif in_cli and _CLI_END_CHAPTER_PATTERN.search(text.replace(" ", "")):
                        chunk_counter = self._flush(
                            collector, chunks, file_name,
                            current_section, chunk_counter,
                        )
                        collector.reset()
                        in_cli = False
                    continue

                if not in_cli:
                    continue

                # ---- 子节标题 (18pt) ----
                if level == "section":
                    chunk_counter = self._flush(
                        collector, chunks, file_name,
                        current_section, chunk_counter,
                    )
                    collector.reset()
                    current_section = text
                    chunk_counter += 1
                    chunks.append(self._build_chunk(
                        text=f"章节: {text}",
                        file_name=file_name,
                        chunk_id=f"section_{chunk_counter:05d}",
                        chunk_type="section",
                        section=text,
                        doc_type="cli",
                    ))
                    continue

                # ---- 命令标题 (16pt) ----
                if level == "command":
                    if self._should_skip(text):
                        chunk_counter = self._flush(
                            collector, chunks, file_name,
                            current_section, chunk_counter,
                        )
                        collector.reset()
                        continue

                    # flush 上一个命令
                    chunk_counter = self._flush(
                        collector, chunks, file_name,
                        current_section, chunk_counter,
                    )
                    collector.reset()

                    # 处理合并段落
                    if _is_merged_paragraph(segments):
                        title_text, extra_parts = _parse_merged_runs(segments)
                        self._init_collector(title_text, collector)
                        for state, content in extra_parts:
                            collector.set_state(state)
                            collector.add_text(content)
                            collector.update_command_info(content)
                            collector.check_interactive_usage(content)
                    else:
                        self._init_collector(text, collector)

                    continue

                # ---- 标签 (13pt) ----
                if level == "label":
                    new_state = _detect_label_state(text)
                    if new_state:
                        collector.set_state(new_state)
                    # 标签行可能内嵌命令格式
                    syntax_m = _SYNTAX_FULL_RE.search(text)
                    if syntax_m:
                        collector.syntax_lines.append(text.strip())
                        collector.update_command_info(text.strip())
                    continue

                # ---- 正文 / 备注 ----
                if level in ("body", "note"):
                    if self._is_noise_paragraph(text):
                        continue
                    collector.add_text(text)
                    collector.update_command_info(text)
                    collector.check_interactive_usage(text)

            elif elem_type == "table":
                if not in_cli:
                    continue
                table = Table(elem, doc)
                if self._is_noise_table(table):
                    continue

                table_text = self._format_table(table)
                collector.add_table(table_text)
                collector.update_command_info(table_text)

        # flush 最后一条
        chunk_counter = self._flush(
            collector, chunks, file_name,
            current_section, chunk_counter,
        )

        logger.info(f"解析完成: {file_name}, 共 {len(chunks)} 个 chunks")
        return chunks

    # ======================================================================
    # flush
    # ======================================================================

    def _flush(
        self,
        collector: CommandCollector,
        chunks: List[Dict[str, Any]],
        file_name: str,
        section: str,
        chunk_counter: int,
    ) -> int:
        if not collector.is_collecting:
            return chunk_counter

        title = collector.title.strip()
        if len(title) < 5:
            return chunk_counter

        chunk_counter += 1

        # -- 提取 command_name / subcommand / full_command --
        self._finalize_command_info(collector)

        # -- 构建 text --
        parts: List[str] = []

        # 命令标识行 (URI-first 风格)
        cmd_header = self._build_command_header(collector)
        parts.append(cmd_header)

        # 同义词
        synonyms = self._get_synonyms(collector)
        if synonyms:
            parts.append(f"同义词: {synonyms}")

        # 功能描述
        func = "\n".join(collector.function_lines).strip()
        if func:
            parts.append(f"命令功能:\n{func}")

        # 语法
        syntax = "\n".join(collector.syntax_lines).strip()
        if syntax:
            parts.append(f"命令格式:\n{syntax}")

        # 参数说明
        if collector.parameter_tables:
            parts.append("参数说明:\n" + "\n\n".join(collector.parameter_tables))

        # 使用指南
        usage = "\n".join(collector.usage_lines).strip()
        if usage:
            parts.append(f"使用指南:\n{usage}")

        # 使用实例
        if collector.example_tables:
            parts.append("使用实例:\n" + "\n\n".join(collector.example_tables))

        # 交互式提示
        if collector.is_interactive:
            parts.append("注意: 此命令为交互式命令，执行后需要输入密码或确认信息。")

        full_text = "\n\n".join(parts)
        if len(full_text.strip()) < 10:
            return chunk_counter

        # -- 构建 metadata --
        section_path = f"{section} > {title}" if section and title else (title or section)

        meta_extra: Dict[str, Any] = {
            "interactive": collector.is_interactive,
            "description": (func or title)[:200],
        }
        if collector.command_name:
            meta_extra["command_name"] = collector.command_name
        if collector.subcommand:
            meta_extra["subcommand"] = collector.subcommand
        if collector.full_command:
            meta_extra["full_command"] = collector.full_command[:500]
        if syntax:
            meta_extra["syntax"] = syntax[:500]
        if collector.parameter_tables:
            meta_extra["parameters"] = "\n".join(collector.parameter_tables)[:500]
        if collector.example_tables:
            meta_extra["example"] = "\n".join(collector.example_tables)[:500]
        if collector.interaction_steps:
            meta_extra["interaction_steps"] = collector.interaction_steps
        if collector.chinese_name:
            meta_extra["chinese_name"] = collector.chinese_name
        if collector.english_name:
            meta_extra["english_name"] = collector.english_name

        chunks.append(self._build_chunk(
            text=full_text,
            file_name=file_name,
            chunk_id=f"cmd_{chunk_counter:05d}",
            chunk_type="command",
            section=section_path[:500],
            doc_type="cli",
            **meta_extra,
        ))

        logger.debug(
            f"Flush: [cmd_{chunk_counter:05d}] "
            f"cmd={meta_extra.get('command_name', '-')} "
            f"sub={meta_extra.get('subcommand', '-')} "
            f"interactive={collector.is_interactive} "
            f"steps={len(collector.interaction_steps)} "
            f"| {title[:60]}"
        )
        return chunk_counter

    # ======================================================================
    # 命令信息提取
    # ======================================================================

    @staticmethod
    def _init_collector(title: str, collector: CommandCollector) -> None:
        """从标题文本初始化收集器."""
        collector.title = title.strip()
        m = _TITLE_RE.match(title.strip())
        if m:
            collector.section_number = m.group("num") or ""
            cn_raw = m.group("cn") or ""
            en_raw = m.group("en") or ""
            collector.chinese_name = re.sub(r"^\d+[\.\d]*\s+", "", cn_raw).strip()
            collector.english_name = en_raw.strip()

            # 从英文括号名提取 subcommand
            if en_raw:
                en_stripped = en_raw.strip()
                # 复合: "service -d state" -> cmd=ipmcset, sub=-d state
                compound = _COMPOUND_EN_RE.match(en_stripped)
                if compound:
                    collector.command_name = ""  # 由语法行覆盖 (ipmcset/ipmcget)
                    collector.subcommand = f"-d {compound.group(2).lower()}"
                    collector.full_command = en_stripped
                elif "/" in en_stripped:
                    # "userlist/list" -> 取第一个
                    first = en_stripped.split("/")[0].strip()
                    collector.subcommand = f"-d {first.lower()}"
                else:
                    collector.subcommand = f"-d {en_stripped.lower()}"
        else:
            collector.chinese_name = title.strip()

    @staticmethod
    def _finalize_command_info(collector: CommandCollector) -> None:
        """在 flush 前做最终命令信息提取 (从语法行覆盖)."""
        # 语法行有最准确的 command_name + subcommand
        best_syntax = ""
        for line in collector.syntax_lines:
            # 逐行搜索, 跳过标签前缀
            for sub_line in line.split("\n"):
                sub_line = sub_line.strip()
                if not sub_line:
                    continue
                m = _SYNTAX_FULL_RE.search(sub_line)
                if m:
                    collector.command_name = m.group(1).lower()
                    collector.subcommand = f"-d {m.group(3).lower()}"
                    best_syntax = sub_line
                    break
            if best_syntax:
                break
        if best_syntax:
            collector.full_command = best_syntax

        # 兜底: 从 example 表格提取 command_name
        if not collector.command_name:
            for tbl in collector.example_tables:
                m = _CLI_CMD_RE.search(tbl)
                if m:
                    collector.command_name = m.group(1).lower()
                    # 同时从示例中尝试提取 subcommand
                    if not collector.subcommand:
                        sm = re.search(r"-d\s+(\S+)", tbl)
                        if sm:
                            collector.subcommand = f"-d {sm.group(1).lower()}"
                    break

        # 兜底: 从标题英文括号提取 command_name
        if not collector.command_name and collector.english_name:
            compound = _COMPOUND_EN_RE.match(collector.english_name.strip())
            if not compound:
                # 单名如 "adduser", "ipaddr" -> 默认 ipmcset
                collector.command_name = "ipmcset"

        # 构建 full_command
        if not collector.full_command and collector.command_name and collector.subcommand:
            collector.full_command = f"{collector.command_name} {collector.subcommand}"

    @staticmethod
    def _build_command_header(collector: CommandCollector) -> str:
        """构建命令标识行 (URI-first 风格, 命令名前置)."""
        parts = [f"命令: {collector.title}"]
        if collector.command_name and collector.subcommand:
            parts.append(f"CLI命令: {collector.command_name} {collector.subcommand}")
        elif collector.command_name:
            parts.append(f"CLI命令: {collector.command_name}")
        return "\n".join(parts)

    # ======================================================================
    # 同义词生成
    # ======================================================================

    @staticmethod
    def _get_synonyms(collector: CommandCollector) -> str:
        """根据命令标题、描述、英文括号名生成丰富同义词."""
        synonyms: Set[str] = set()

        # 1. 从标题 + 描述中匹配同义词关键词
        search_text = f"{collector.title} {' '.join(collector.function_lines)}"
        for keyword, synonym_str in _SYNONYM_MAP.items():
            if keyword in search_text:
                for s in synonym_str.split():
                    if len(s) > 1:
                        synonyms.add(s)

        # 2. 从英文括号名拆分
        if collector.english_name:
            en = collector.english_name.strip()
            # 复合: "service -d state" -> ["service", "state"]
            compound = _COMPOUND_EN_RE.match(en)
            if compound:
                synonyms.add(compound.group(1).lower())
                synonyms.add(compound.group(2).lower())
            elif "/" in en:
                for part in en.split("/"):
                    p = part.strip().lower()
                    if len(p) > 1:
                        synonyms.add(p)
            else:
                synonyms.add(en.lower())

        # 3. 从 subcommand 提取核心词
        if collector.subcommand:
            m = re.search(r"-d\s+(\S+)", collector.subcommand)
            if m:
                synonyms.add(m.group(1).lower())

        # 4. 从 command_name 添加
        if collector.command_name:
            synonyms.add(collector.command_name)

        # 5. 从标题中文名提取核心动词
        cn = collector.chinese_name
        if cn:
            for verb in ["查询", "设置", "添加", "删除", "修改", "清除", "导入", "导出",
                         "启用", "禁用", "锁定", "解锁", "恢复", "更新", "测试"]:
                if verb in cn:
                    synonyms.add(verb)

        return " ".join(sorted(synonyms))

    # ======================================================================
    # 内部工具方法
    # ======================================================================

    @staticmethod
    def _iter_body_elements(doc: Document):
        body = doc.element.body
        for child in body:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "p":
                yield "paragraph", child
            elif tag == "tbl":
                yield "table", child

    @staticmethod
    def _classify_by_runs(segments: List[RunSegment]) -> Tuple[str, str]:
        """基于 run 级字号的最大值对段落分类."""
        text = "".join(s.text for s in segments).strip()
        if not text:
            return "empty", text

        max_pt = max(s.pt for s in segments if s.text.strip()) if segments else 0

        if max_pt >= FONT_SIZE_CHAPTER:
            return "chapter", text
        elif max_pt >= FONT_SIZE_SECTION:
            return "section", text
        elif max_pt >= FONT_SIZE_COMMAND:
            # 16pt 段落: 判断是否为真正的命令标题
            if _looks_like_command_title(text):
                return "command", text
            else:
                # 列表项或正文被误标为 16pt, 降级为 body
                return "body", text
        elif max_pt >= FONT_SIZE_LABEL:
            return "label", text
        elif max_pt >= 9.5:
            return "body", text
        else:
            return "note", text

    @staticmethod
    def _should_skip(text: str) -> bool:
        for skip in _SKIP_TITLES:
            if skip in text:
                return True
        return False

    @staticmethod
    def _is_noise_table(table: Table) -> bool:
        num_rows = len(table.rows)
        if num_rows == 0:
            return True

        all_text = " ".join(
            cell.text.strip()
            for row in table.rows
            for cell in row.cells
        )

        if num_rows <= 2:
            for kw in _NOISE_TABLE_KEYWORDS:
                if kw in all_text:
                    return True

        if _TOC_DOT_PATTERN.search(all_text):
            return True

        if "文档版本" in all_text and num_rows <= 3:
            return True

        if "X-Auth-Token" in all_text and num_rows <= 3:
            return True

        return False

    @staticmethod
    def _is_noise_paragraph(text: str) -> bool:
        for kw in _NOISE_TABLE_KEYWORDS:
            if kw in text:
                return True
        if text.startswith("文档版本") or text.startswith("发布日期"):
            return True
        return False

    @staticmethod
    def _format_table(table: Table) -> str:
        rows_text: List[str] = []
        seen: Set[int] = set()
        for row in table.rows:
            cells_text: List[str] = []
            for cell in row.cells:
                tc_id = id(cell._tc)
                if tc_id in seen:
                    continue
                seen.add(tc_id)
                cells_text.append(cell.text.strip())
            if cells_text:
                rows_text.append(" | ".join(cells_text))
        return "\n".join(rows_text)
