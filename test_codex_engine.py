"""Codex 执行器接入测试。

重点覆盖三类容易出错的地方：
  1. 命令行拼装 —— codex 与 claude 参数完全不同，不能共用模板；
  2. 事件解析 —— codex 是 JSONL 且没有流式增量，正文分多次给出必须累加；
  3. 失败判定 —— codex 用 turn.failed 表达失败，退出码未必非零。
"""
import ast
import os
import unittest
from pathlib import Path

SOURCE = Path(__file__).with_name('bot.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)


def _const(name):
    for node in TREE.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError('未找到常量 ' + name)


ENGINES = _const('ENGINES')


def _func(name):
    """取出函数源码文本，便于断言关键语句存在。"""
    parts = SOURCE.split('def ' + name + '(', 1)
    if len(parts) != 2:
        raise AssertionError('未找到函数 ' + name)
    body = parts[1]
    # 截到下个顶层 def 为止
    lines = body.split('\n')
    out = []
    for line in lines[1:]:
        if line.startswith('def ') or line.startswith('class '):
            break
        out.append(line)
    return '\n'.join(out)


def _strip_comments(text):
    """去掉整行注释，避免把「这里没有某参数」的说明文字当成代码证据。"""
    return '\n'.join(line for line in text.split('\n') if not line.strip().startswith('#'))


class EngineRegistryTest(unittest.TestCase):
    def test_codex_registered(self):
        self.assertIn('codex', ENGINES)
        self.assertEqual(ENGINES['codex']['kind'], 'codex')

    def test_existing_engines_unchanged(self):
        """加执行器不能顺手改坏原有的两个。"""
        self.assertEqual(ENGINES['claude']['kind'], 'claude')
        self.assertEqual(ENGINES['codebuddy']['kind'], 'claude')
        self.assertEqual(ENGINES['codebuddy']['binary'], 'codebuddy')

    def test_every_engine_has_required_fields(self):
        for name, spec in ENGINES.items():
            for field in ('label', 'kind', 'binary', 'button'):
                self.assertIn(field, spec, name + ' 缺少字段 ' + field)

    def test_buttons_are_unique(self):
        buttons = [spec['button'] for spec in ENGINES.values()]
        self.assertEqual(len(buttons), len(set(buttons)), '执行器按钮名重复')


class CodexCommandTest(unittest.TestCase):
    def test_command_uses_exec_subcommand(self):
        """codex 必须走 exec 子命令，不能套 claude 的 -p。"""
        body = _func('engine_command')
        self.assertIn("'exec'", body)
        self.assertIn("'--json'", body)

    def test_command_skips_git_repo_check(self):
        """工作目录不保证是 git 仓库，缺这个参数 codex 直接拒绝运行。"""
        self.assertIn("'--skip-git-repo-check'", _func('engine_command'))

    def test_command_passes_working_dir(self):
        """codex 不认 cwd 之外的工作目录，必须显式 --cd。"""
        self.assertIn("'--cd'", _func('engine_command'))

    def test_instruction_goes_into_prompt(self):
        """codex 没有 --append-system-prompt，指令必须拼进 prompt，否则约束全丢。"""
        body = _strip_comments(_func('engine_command'))
        codex_branch = body.split("spec['kind'] == 'codex':")[1].split('return')[0]
        self.assertNotIn('--append-system-prompt', codex_branch)
        self.assertIn('instructions + ', _func('engine_command'))

    def test_codex_does_not_send_claude_only_flags(self):
        """把 --max-turns / --include-partial-messages 传给 codex 会直接报参数错误。"""
        body = _strip_comments(_func('engine_command'))
        codex_branch = body.split("if spec['kind'] == 'codex':")[1].split("command += ['-p'")[0]
        for flag in ('--max-turns', '--include-partial-messages', '--dangerously-skip-permissions',
                     '--output-format', '--append-system-prompt'):
            self.assertNotIn(flag, codex_branch, 'codex 分支不应出现 ' + flag)

    def test_claude_branch_keeps_original_flags(self):
        body = _func('engine_command')
        claude_branch = body.split("command += ['-p'")[1]
        for flag in ('--output-format', '--max-turns', '--append-system-prompt'):
            self.assertIn(flag, claude_branch)


class CodexEventParseTest(unittest.TestCase):
    """直接跑解析函数的真实逻辑，不是只看源码里有没有写。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location('botmod_under_test', Path(__file__).with_name('bot.py'))
        module = importlib.util.module_from_spec(spec)
        # bot.py 在导入期会读环境并建目录，这里给最小可用的环境。
        os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'TEST-ONLY-0000')
        os.environ.setdefault('CLAUDE_BOT_WORKDIR', '.testwork')
        try:
            spec.loader.exec_module(module)
        except SystemExit:
            raise unittest.SkipTest('bot.py 在导入期退出，跳过真实解析测试')
        cls.module = module

    def new_state(self):
        return {'content': '', 'final': None, 'activity': '等待模型响应', 'failed': ''}

    def test_agent_message_accumulates(self):
        state = self.new_state()
        parse = self.module.parse_codex_event
        parse({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '第一段'}}, state)
        parse({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '第二段'}}, state)
        self.assertIn('第一段', state['content'])
        self.assertIn('第二段', state['content'])
        self.assertEqual(state['content'].count('\n'), 2,
                         '两段正文之间应当只补一个空行，实际换行数不对')

    def test_agent_message_separator_ignores_sentence_end(self):
        """codex 的分段不能借用 claude 那套「看句末标点」的逻辑。

        agent_message 是整条消息，模型完全可能写「第一段」这种不成句的内容。
        如果这里也跟着按标点判断，两段会被空格黏成「第一段 第二段」——
        看起来就像根本没换行（2026-10-26 引入句子边界判断时踩到过）。
        """
        for text in ('第一段', '第一段。', '结尾是数字 32'):
            self.assertEqual(self.module.agent_message_separator(text), '\n\n',
                             'codex 的两条消息之间必须分段：' + text)

    def test_claude_block_boundary_does_use_sentence_end(self):
        """反向钉住：claude 那边仍然要按句子边界判断，两套逻辑不能合并。"""
        self.assertEqual(self.module.start_text_block('结尾是数字 32'), '',
                         'claude 的块边界断在数字中间时既不分段也不补空格')
        self.assertEqual(self.module.start_text_block('这一段说完了。'), '\n\n')

    def test_reasoning_does_not_pollute_content(self):
        """推理摘要不能混进正文，否则用户看到一堆思考内容。"""
        state = self.new_state()
        self.module.parse_codex_event(
            {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': '我在想…'}}, state)
        self.assertEqual(state['content'], '')
        self.assertEqual(state['activity'], '正在推理')

    def test_command_execution_updates_activity(self):
        state = self.new_state()
        self.module.parse_codex_event(
            {'type': 'item.completed', 'item': {'type': 'command_execution',
                                                'command': '/bin/bash -lc uptime',
                                                'exit_code': 0}}, state)
        self.assertIn('uptime', state['activity'])
        self.assertIn('0', state['activity'])

    def test_thread_started_saves_session(self):
        state = self.new_state()
        saved = []
        original = self.module.save_session
        self.module.save_session = lambda value: saved.append(value)
        try:
            self.module.parse_codex_event({'type': 'thread.started', 'thread_id': 'abc-123'}, state)
        finally:
            self.module.save_session = original
        self.assertEqual(saved, ['abc-123'])

    def test_turn_failed_sets_failed(self):
        state = self.new_state()
        self.module.parse_codex_event(
            {'type': 'turn.failed', 'error': {'message': '额度不足'}}, state)
        self.assertEqual(state['failed'], '额度不足')

    def test_error_item_alone_is_not_failure(self):
        """配置告警类 error item 不该被当成整轮失败，否则每轮都误报。"""
        state = self.new_state()
        self.module.parse_codex_event(
            {'type': 'item.completed',
             'item': {'type': 'error', 'message': 'Codex is ignoring 2 unrecognized settings'}},
            state)
        self.assertEqual(state['failed'], '')

    def test_turn_completed_is_not_failure(self):
        state = self.new_state()
        self.module.parse_codex_event(
            {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2}}, state)
        self.assertEqual(state['failed'], '')


class CodexEnvironmentMappingTest(unittest.TestCase):
    """Codex 只认 OPENAI_* 变量，本站凭据存在 ANTHROPIC_* 下。

    这一段做过真实对照实验：只映射 KEY 恰好也能跑通，因为本站网关同时是 OpenAI 兼容端点；
    但换成只对别的域名开放 OpenAI 协议的网关就会静默打到官方地址。所以两条映射都必须有。
    这里真实调用 engine_command，验证 environment 字典的最终内容。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location('botmod_env_test', Path(__file__).with_name('bot.py'))
        module = importlib.util.module_from_spec(spec)
        os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'TEST-ONLY-0000')
        os.environ.setdefault('CLAUDE_BOT_WORKDIR', '.testwork')
        try:
            spec.loader.exec_module(module)
        except SystemExit:
            raise unittest.SkipTest('bot.py 在导入期退出，跳过环境映射测试')
        cls.module = module

    def build(self, environment):
        # 关掉会话续接与模型覆盖，让命令只受 environment 影响。
        original_session, original_model = self.module.session_id, self.module.current_model
        self.module.session_id = lambda: None
        self.module.current_model = lambda: ''
        try:
            command, prompt = self.module.engine_command('codex', '任务正文', '指令', 1, environment)
        finally:
            self.module.session_id, self.module.current_model = original_session, original_model
        return command, prompt, environment

    def test_api_key_is_mapped(self):
        env = {'ANTHROPIC_API_KEY': 'FAKE-TEST-KEY'}
        self.build(env)
        self.assertEqual(env.get('OPENAI_API_KEY'), 'FAKE-TEST-KEY',
                         'codex 只读 OPENAI_API_KEY，缺映射就等于没凭据')

    def test_base_url_is_mapped_with_v1(self):
        env = {'ANTHROPIC_BASE_URL': 'https://gateway.example.com', 'ANTHROPIC_API_KEY': 'FAKE'}
        self.build(env)
        self.assertEqual(env.get('OPENAI_BASE_URL'), 'https://gateway.example.com/v1',
                         '网关地址也必须映射，否则 codex 会退回官方 endpoint')

    def test_base_url_does_not_double_slash(self):
        env = {'ANTHROPIC_BASE_URL': 'https://gateway.example.com/'}
        self.build(env)
        self.assertNotIn('//v1', env.get('OPENAI_BASE_URL', ''), '末尾斜杠要归一，不能拼成 //v1')

    def test_existing_openai_vars_win(self):
        """运维显式配了 OPENAI_* 时不能被覆盖，否则无法把 Codex 指到别的网关。"""
        env = {'ANTHROPIC_API_KEY': 'FAKE-A', 'ANTHROPIC_BASE_URL': 'https://a.example.com',
               'OPENAI_API_KEY': 'FAKE-O', 'OPENAI_BASE_URL': 'https://b.example.com/v1'}
        self.build(env)
        self.assertEqual(env['OPENAI_API_KEY'], 'FAKE-O')
        self.assertEqual(env['OPENAI_BASE_URL'], 'https://b.example.com/v1')

    def test_command_uses_mapped_environment(self):
        env = {'ANTHROPIC_API_KEY': 'FAKE-TEST-KEY', 'ANTHROPIC_BASE_URL': 'https://gateway.example.com'}
        command, prompt, _ = self.build(env)
        self.assertEqual(command[0].split('/')[-1], 'codex')
        self.assertEqual(command[1], 'exec')
        self.assertIn('指令', prompt)
        self.assertIn('任务正文', prompt)


class FailureJudgementTest(unittest.TestCase):
    def test_failed_state_counts_as_error(self):
        """turn.failed 时退出码可能仍是 0，必须把 state 的失败标记算进来。

        只看源码里有没有 state['failed'] 字样是不够的——把它挂在 `or False and ...`
        里也照样出现。这里直接求值那段判断表达式，验证它真的会变 True。
        """
        body = _strip_comments(_func('run_stream'))
        line = next((item for item in body.split('\n') if item.strip().startswith('crashed =')), '')
        self.assertTrue(line, '未找到失败判定语句')
        expression = line.split('=', 1)[1].strip()
        self.assertIn("state['failed']", expression, '失败判定必须包含 codex 的失败标记')

        class FakeProcess:
            returncode = 0        # 关键：codex 失败时退出码常常仍是 0

        namespace = {'process': FakeProcess(), 'final': None,
                     'state': {'failed': '额度不足'}}
        self.assertTrue(eval(expression, {}, namespace),
                        '退出码为 0 但 state 有失败标记时，必须判定为失败')

        namespace['state'] = {'failed': ''}
        self.assertFalse(eval(expression, {}, namespace),
                         '没有任何失败迹象时不能误报失败')

    def test_failure_reason_shown_when_no_content(self):
        body = _func('run_stream')
        self.assertIn('state[\'failed\']', body)


class EngineSpecFallbackTest(unittest.TestCase):
    def test_unknown_engine_does_not_crash(self):
        """旧状态文件里残留未注册的引擎名时，不能抛 KeyError 把任务卡死。"""
        node = None
        for item in TREE.body:
            if isinstance(item, ast.FunctionDef) and item.name == 'engine_spec':
                node = item
        self.assertIsNotNone(node, '未找到 engine_spec')
        source = ast.get_source_segment(SOURCE, node)
        self.assertIn('if name in ENGINES', source)
        self.assertIn("ENGINES['claude']", source, '必须有兜底，不能返回 None')


if __name__ == '__main__':
    unittest.main(verbosity=2)
