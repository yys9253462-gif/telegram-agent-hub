#!/usr/bin/env python3
"""回复文本的纯函数：电报 HTML 排版、按上限切片、空行整理、脱敏、耗时格式。

都不依赖机器人状态、不发网络请求，从 bot.py 拆出来便于单独测试；
bot.py 原样导入这些名字，`bot.render_reply` 等旧引用不受影响。
"""
import html
import re


def format_elapsed(seconds):
    """把秒数写成「37秒」「2分14秒」「1小时3分」这类人读的时长。

    负数一律当 0：墙钟会被 NTP 校准往回拨，算出来 -1 秒纯属时钟问题，
    显示成一个负的耗时只会让人以为出错了。
    """
    try:
        total = int(max(0, float(seconds)))
    except (TypeError, ValueError):
        total = 0
    if total < 60:
        return str(total) + '秒'
    if total < 3600:
        return str(total // 60) + '分' + str(total % 60) + '秒'
    hours, rest = divmod(total, 3600)
    return str(hours) + '小时' + str(rest // 60) + '分'


# 结构标记：bot.py 拼回复时用它们标出「任务标题」「进度行」「执行信息」三种块，
# render_reply 据此套样式。用不可见分隔符包住，正常文字里不会出现；纯文本兜底时剥掉。
TITLE_MARK = '\u2063title\u2063'
PROGRESS_MARK = '\u2063progress\u2063'
FOOTER_MARK = '\u2063footer\u2063'
MARKS = (TITLE_MARK, PROGRESS_MARK, FOOTER_MARK)


def strip_marks(text):
    for mark in MARKS:
        text = text.replace(mark + '\n', '').replace(mark, '')
    return text


def render_reply(text):
    """把模型的 Markdown 风格正文转成电报 HTML，并给各层套上能区分的样式：

    - 任务标题：📌 加粗；正文标题：一二级 🟦、三级以下 🔹，加粗；
    - 进度行：⏳ 斜体；执行信息与用量：可折叠引用块（彩色竖条，点开看全部）；
    - 正文里的 > 引用：引用块；- / * 列表：• ；带语言的代码块：语法高亮。
    """
    def inline(value):
        tokens = re.split(r'(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\(https?://[^\s)]+\))', value)
        output = []
        for token in tokens:
            if token.startswith('`') and token.endswith('`') and len(token) > 2:
                output.append('<code>' + html.escape(token[1:-1]) + '</code>')
            elif token.startswith('**') and token.endswith('**') and len(token) > 4:
                output.append('<b>' + html.escape(token[2:-2]) + '</b>')
            else:
                link = re.fullmatch(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)', token)
                if link:
                    output.append('<a href="' + html.escape(link[2], quote=True) + '">' + html.escape(link[1]) + '</a>')
                else:
                    output.append(html.escape(token))
        return ''.join(output)

    output = []
    code = []
    fence = None
    language = ''
    quote = []
    footer = None

    def flush_quote():
        if quote:
            output.append('<blockquote>' + '\n'.join(quote) + '</blockquote>')
            quote.clear()

    for line in text.splitlines():
        stripped = line.lstrip()
        if footer is not None:
            if stripped:
                footer.append(inline(line))
            continue
        if stripped.startswith(('```', '~~~')):
            mark = stripped[:3]
            if fence is None:
                flush_quote()
                fence = mark
                code = []
                language = re.sub(r'[^A-Za-z0-9_+-]', '', stripped[3:].strip())[:20]
                continue
            if mark == fence:
                body = html.escape('\n'.join(code))
                output.append('<pre><code class="language-' + language + '">' + body + '</code></pre>'
                              if language else '<pre>' + body + '</pre>')
                fence = None
                continue
        if fence is not None:
            code.append(line)
            continue
        if stripped == FOOTER_MARK:
            flush_quote()
            footer = []
            continue
        if stripped.startswith('>'):
            quote.append(inline(stripped[1:].lstrip()))
            continue
        flush_quote()
        if stripped.startswith(TITLE_MARK):
            output.append('<b>📌 ' + inline(stripped[len(TITLE_MARK):]) + '</b>')
            continue
        if stripped.startswith(PROGRESS_MARK):
            output.append('<i>⏳ ' + inline(stripped[len(PROGRESS_MARK):]) + '</i>')
            continue
        heading = re.match(r'^(#{1,6})\s+(.+)', stripped)
        if heading:
            icon = '🟦 ' if len(heading[1]) <= 2 else '🔹 '
            output.append(icon + '<b>' + inline(heading[2]) + '</b>')
        elif re.fullmatch(r'[-*_]{3,}', stripped):
            output.append('────────────')
        elif re.match(r'^[-*]\s+\S', stripped):
            indent = '  ' * min(3, (len(line) - len(stripped)) // 2)
            output.append(indent + '• ' + inline(stripped[1:].lstrip()))
        else:
            output.append(inline(line))
    flush_quote()
    if fence is not None:
        output.append('<pre>' + html.escape('\n'.join(code)) + '</pre>')
    if footer:
        output.append('<blockquote expandable>' + '\n'.join(footer) + '</blockquote>')
    return '\n'.join(output).strip() or '正在生成…'


def split_message(text, limit=1800):
    """按行切分长文本，避免在行中间截断（硬切会破坏代码块与 HTML 转义边界）。

    单行本身超长时退化为切分，但优先在行内标记（** 或 `）的空隙处断开，
    否则会把加粗劈成两半、把代码符号直接暴露给用户。

    limit 是**原文**字符数上限。真正的硬约束是渲染成 HTML 后的长度
    （Telegram 上限 4096），而 HTML 转义会把 & < > 各撑到 5/4/4 倍，
    一片 1800 字元的原文完全可能渲染成 9000 字符而被拒。所以这里在
    切片后追加一道按渲染长度的复查，超限就继续对半拆。
    """
    pieces = []
    buffer = ''
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if buffer:
                pieces.append(buffer)
                buffer = ''
            rest = line
            while len(rest) > limit:
                cut = 0
                # 超长单行：退一步在行内标记的空隙处断开，避免劈开 **` 等符号。
                for pattern in (r'\*\*', r'`'):
                    positions = [m.start() for m in re.finditer(pattern, rest[:limit]) if m.start() > limit // 2]
                    if positions:
                        cut = positions[-1]
                        break
                if cut <= 0:
                    space = rest.rfind(' ', 0, limit)
                    cut = space if space > limit // 2 else limit
                pieces.append(rest[:cut])
                rest = rest[cut:]
            buffer = rest
            continue
        if len(buffer) + len(line) > limit:
            pieces.append(buffer)
            buffer = ''
        buffer += line
    if buffer:
        pieces.append(buffer)
    if not pieces:
        return ['']
    # 逐片检查围栏是否成对；未闭合则在本片末尾补上收尾，并在下一片开头重新打开。
    balanced = []
    opened = None
    for piece in pieces:
        body = piece if opened is None else opened + '\n' + piece
        fences = [line.lstrip()[:3] for line in body.splitlines()
                  if line.lstrip().startswith(('```', '~~~'))]
        state = None
        for mark in fences:
            state = mark if state is None else (None if mark == state else state)
        if state is not None:
            body = body + '\n' + state
            opened = state
        else:
            opened = None
        balanced.append(body)
    return clamp_rendered(balanced)


# Telegram 单条消息上限 4096 字符，这里留出余量给 <b>/<pre> 等标签本身。
RENDER_LIMIT = 3800


def clamp_rendered(pieces):
    """确保每片渲染成 HTML 后不超过硬上限，超限的对半拆开再复查。

    递归深度受对数约束：1800 字的原文最多拆到 1 字，约 11 层就必然收敛，
    不会因为某个字符恒定膨胀而无限递归。
    """
    result = []
    for piece in pieces:
        if len(render_reply(piece)) <= RENDER_LIMIT or len(piece) <= 1:
            result.append(piece)
            continue
        middle = len(piece) // 2
        # 优先在换行处断开，让两半各自仍是完整的行。
        cut = piece.rfind('\n', 0, middle)
        if cut <= 0:
            cut = middle
        result.extend(clamp_rendered([piece[:cut], piece[cut:]]))
    return result


def clean_reply(text):
    text = strip_marks(text)
    lines = []
    fenced = False
    marker = ''
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            current = stripped[:3]
            if not fenced:
                fenced, marker = True, current
            elif current == marker:
                fenced = False
            lines.append(line)
            continue
        if fenced or line.startswith(('    ', '\t')):
            lines.append(line)
            continue
        line = re.sub(r'^([ ]{0,3})#{1,6}[ \t]+', r'\1', line)
        parts = re.split(r'(`+[^`]*`+)', line)
        for index in range(0, len(parts), 2):
            parts[index] = re.sub(r'\*\*(?=\S)(.+?)(?<=\S)\*\*', r'\1', parts[index])
        lines.append(''.join(parts))
    return ''.join(lines)


def tidy_stream_text(text):
    """压缩流式累积过程中产生的多余空行。

    只处理空行密度，不改动正文：连续 3 个以上换行压成 2 个（最多留一个空行），
    并去掉首尾多余的换行。正文中间正常的段落分隔（2 个换行）原样保留。
    """
    return re.sub(r'\n{3,}', '\n\n', text).strip('\n')


# 机器人自己持有的密钥原文（电报令牌、各供应商 API 密钥、平台授权令牌）。
# 正则只认得带固定前缀的形态；无前缀的网关密钥一旦被模型 echo 出来，
# 只有按原值匹配才能挡住。由 bot.py 在读取/注入密钥时登记。
SECRET_VALUES = set()


def register_secret(value):
    value = str(value or '').strip()
    # 太短的值按原文替换会误伤正常文字，交给正则规则处理。
    if len(value) >= 12:
        SECRET_VALUES.add(value)


def redact(value):
    """抹掉文本里的凭证。顺序有讲究：先处理更长、更具体的形态，
    否则宽泛规则会先吃掉前缀，让后面的规则再也匹配不到令牌本体。"""
    value = str(value)
    # 长的先换：一枚密钥若是另一枚的前缀，先换短的会留下半截。
    for secret in sorted(SECRET_VALUES, key=len, reverse=True):
        if secret in value:
            value = value.replace(secret, '[已隐藏密钥]')
    value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----', '[已隐藏私钥]', value)
    # URL 内嵌凭证 https://user:password@host 与数据库连接串，必须在通用规则之前处理。
    # 密码里可能含 @，因此匹配到「最后一个 @ 之前」的全部内容。
    #
    # 这条规则原来写成 ([a-z][a-z0-9+.-]*://)([^\s/:@]+):([^\s/]+)@，在「有 :// 但没有 @」
    # 的长文本上会退化成二次方回溯：([^\s/]+) 每失败一次都要把 (?:[a-z0-9+.-]*) 重试一遍。
    # 实测 10 万字符的 'sk-'*20000 要 7.9 秒，而这条规则对每条消息、每条结果都会跑。
    # 改用显式字符类 + 独占量词（atomic 语义由尾部 [^\s/]+ 的互斥写法保证），
    # 并把协议头的重复次数收敛到 32（正常协议名不会有 32 个字符）。
    value = re.sub(r'(?i)\b([a-z][a-z0-9+.-]{0,31}://)([^\s/:@]+):([^\s/]+)@', r'\1\2:[已隐藏]@', value)
    # Bearer 必须先于 authorization 规则，否则只抹掉 "Bearer" 而令牌仍留在原地。
    value = re.sub(r'(?i)\bBearer\s+[^\s"\x27,;]+', 'Bearer [已隐藏]', value)
    # 「密钥」「密码」这两个中文词既出现在真正的凭证赋值里（密钥=sk-xxx），
    # 也大量出现在正常说明里（接口密钥：使用已配置的网关与密钥）。
    # 原来把它们和 api_key 写在同一组，说明句会被整段抹成「接口密钥：[已隐藏]」。
    # 因此中文词单独一条，并要求右边确实像一个凭证：无空格的长串且含数字。
    value = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|passwd|secret)["\x27]?\s*[=:：]\s*["\x27]?)([^\s,;"\x27]+)', r'\1[已隐藏]', value)
    value = re.sub(r'((?:密钥|密码|口令)["\x27]?\s*[=:：]\s*["\x27]?)'
                   r'((?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{8,})', r'\1[已隐藏]', value)
    # 纯数字口令（如「密码：123456」）上面那条不命中，单独兜一次：
    # 六位以上的纯数字在说明句里几乎不会跟在「密钥：」后面，误伤面很小。
    value = re.sub(r'((?:密钥|密码|口令)["\x27]?\s*[=:：]\s*["\x27]?)(\d{6,})', r'\1[已隐藏]', value)
    value = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|\d{6,}:[A-Za-z0-9_-]{25,})\b', '[已隐藏令牌]', value)
    # 常见云厂商与代码托管平台的固定前缀密钥。
    value = re.sub(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b', '[已隐藏令牌]', value)
    value = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b', '[已隐藏令牌]', value)
    # Cloudflare 令牌：新格式带 cfut_ 前缀。旧格式是无前缀的 40 位字母数字，
    # 与普通哈希无法区分，只能靠上下文关键字兜（上面 api_key/token 那条规则）。
    value = re.sub(r'\bcfut_[A-Za-z0-9_-]{20,}', '[已隐藏令牌]', value)
    value = re.sub(r'\bxox[baprs]-[A-Za-z0-9-]{10,}', '[已隐藏令牌]', value)
    return value
