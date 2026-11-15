"""主机状态增强检查与静默定时巡检模块。"""
import json
import os
import subprocess
import time
from pathlib import Path

# 只读深挖巡检命令清单（均不带管道与修改动作，安全可控）。
EXTENDED_CHECKS = {
    '最近错误日志': [['journalctl', '-p', 'err', '-n', '20', '--no-pager']],
    '大目录排查': [['du', '-xhd', '2', '/var', '/opt']],
    '内存排行': [['ps', '-eo', 'pid,comm,%mem,%cpu,rss', '--sort=-rss']],
}

# 需要截断展示行数的命令前缀（避免进程列表刷爆消息长度上限）。
LINE_CAP_PREFIXES = {
    'ps': 16,
    'du': 25,
}

DEFAULT_PATROL_CONFIG = {
    'enabled': False,
    'interval_minutes': 30,
    'disk_threshold': 85,
    'mem_threshold': 90,
    'cooldown_minutes': 120,
}

HOST_STATUS_QUICK_ACTIONS = [
    ('📜 最近错误日志', '最近错误日志'),
    ('📦 大目录排查', '大目录排查'),
    ('🧠 内存排行', '内存排行'),
    ('🩺 巡检配置', '巡检配置'),
]


def load_patrol_config(path):
    """读取巡检配置，缺项自动补齐默认值；默认关闭主动告警以保持静默。"""
    config = dict(DEFAULT_PATROL_CONFIG)
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if isinstance(data, dict):
            if isinstance(data.get('enabled'), bool):
                config['enabled'] = data['enabled']
            for key, low, high in (
                ('interval_minutes', 5, 1440),
                ('disk_threshold', 50, 99),
                ('mem_threshold', 50, 99),
                ('cooldown_minutes', 15, 1440),
            ):
                value = data.get(key)
                if isinstance(value, int) and low <= value <= high:
                    config[key] = value
    except (OSError, ValueError, TypeError):
        pass
    return config


def save_patrol_config(path, updates, atomic_writer=None):
    """合并更新巡检配置并持久化保存。"""
    config = load_patrol_config(path)
    config.update(updates or {})
    cleaned = dict(DEFAULT_PATROL_CONFIG)
    cleaned['enabled'] = bool(config.get('enabled', False))
    for key, low, high in (
        ('interval_minutes', 5, 1440),
        ('disk_threshold', 50, 99),
        ('mem_threshold', 50, 99),
        ('cooldown_minutes', 15, 1440),
    ):
        value = config.get(key, DEFAULT_PATROL_CONFIG[key])
        if isinstance(value, int):
            cleaned[key] = max(low, min(high, value))
    if atomic_writer is not None:
        atomic_writer(Path(path), cleaned)
    else:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding='utf-8')
    return cleaned


def load_patrol_state(path):
    """读取上次巡检状态记录（活跃告警与提醒时间戳）。"""
    default = {'active': {}, 'last_run': 0.0, 'last_status': '尚未巡检'}
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if isinstance(data, dict):
            active = data.get('active')
            return {
                'active': dict(active) if isinstance(active, dict) else {},
                'last_run': float(data.get('last_run') or 0.0),
                'last_status': str(data.get('last_status') or '尚未巡检'),
            }
    except (OSError, ValueError, TypeError):
        pass
    return default


def save_patrol_state(path, state, atomic_writer=None):
    """保存当前巡检状态以便识别异常新增与恢复。"""
    if atomic_writer is not None:
        atomic_writer(Path(path), state)
    else:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def trim_command_output(command, text):
    """按命令类型限制输出行数，防止长列表刷屏。"""
    lines = [line for line in str(text or '').splitlines()]
    cap = LINE_CAP_PREFIXES.get(command[0]) if command else None
    if cap and len(lines) > cap:
        return '\n'.join(lines[:cap]) + '\n…（仅展示前 ' + str(cap) + ' 行）'
    return '\n'.join(lines)


def sample_memory_and_disk():
    """无需等待 5 秒即可采样的内存与磁盘指标，供定时巡检快速运行。"""
    memory = {}
    for line in Path('/proc/meminfo').read_text(encoding='utf-8').splitlines():
        key, value = line.split(':', 1)
        memory[key] = int(value.split()[0])
    total = memory.get('MemTotal', 0)
    available = memory.get('MemAvailable', memory.get('MemFree', 0))
    used = max(0, total - available)
    mem_pct = used / max(1, total) * 100
    swap_total = memory.get('SwapTotal', 0)
    swap_used = max(0, swap_total - memory.get('SwapFree', 0))

    disk = os.statvfs('/')
    disk_total = disk.f_blocks * disk.f_frsize
    disk_free = disk.f_bavail * disk.f_frsize
    disk_used = (disk.f_blocks - disk.f_bfree) * disk.f_frsize
    disk_pct = disk_used / max(1, disk_used + disk_free) * 100
    try:
        load = ' / '.join(format(value, '.2f') for value in os.getloadavg())
    except (OSError, AttributeError):
        load = '不可用'
    return {
        'mem_total_kb': total,
        'mem_used_kb': used,
        'mem_pct': round(mem_pct, 1),
        'swap_total_kb': swap_total,
        'swap_used_kb': swap_used,
        'disk_total_bytes': disk_total,
        'disk_used_bytes': disk_used,
        'disk_free_bytes': disk_free,
        'disk_pct': round(disk_pct, 1),
        'load': load,
    }


def probe_services(services):
    """检查托管服务是否处于 active 状态。"""
    statuses = {}
    for service in services or ():
        try:
            result = subprocess.run(
                ['systemctl', 'is-active', service],
                capture_output=True, text=True, timeout=6,
            )
            state = (result.stdout or result.stderr or '').strip() or ('active' if result.returncode == 0 else 'inactive')
        except (OSError, subprocess.SubprocessError) as exc:
            state = '探测失败（' + type(exc).__name__ + '）'
        statuses[service] = state
    return statuses


def evaluate_patrol(config, metrics, service_statuses=None, credential_warnings=None):
    """根据当前巡检阈值评估需要触发的异常字典 {键名: 人话描述}。"""
    alerts = {}
    disk_pct = float(metrics.get('disk_pct', 0))
    if disk_pct >= config.get('disk_threshold', 85):
        alerts['disk'] = '根分区磁盘占用已达 ' + format(disk_pct, '.1f') + '%（阈值 ' + str(config.get('disk_threshold', 85)) + '%）'
    mem_pct = float(metrics.get('mem_pct', 0))
    if mem_pct >= config.get('mem_threshold', 90):
        alerts['memory'] = '物理内存占用已达 ' + format(mem_pct, '.1f') + '%（阈值 ' + str(config.get('mem_threshold', 90)) + '%）'
    for service, status in (service_statuses or {}).items():
        if status != 'active':
            alerts['service:' + service] = '服务 ' + service + ' 状态异常：' + str(status)
    for key, msg in (credential_warnings or {}).items():
        alerts['cred:' + key] = str(msg)
    return alerts


def diff_alerts(state, new_alerts, now_ts, cooldown_seconds):
    """计算本轮需要推送的新增/重申告警与已恢复告警，保持平时完全静默。"""
    prev_active = state.get('active', {})
    notify_alerts = {}
    next_active = {}
    recovered = {}

    for key, msg in new_alerts.items():
        old = prev_active.get(key)
        if not old:
            notify_alerts[key] = msg
            next_active[key] = {'message': msg, 'notified_at': now_ts}
        else:
            last_notified = float(old.get('notified_at') or 0.0)
            if now_ts - last_notified >= cooldown_seconds:
                notify_alerts[key] = msg
                next_active[key] = {'message': msg, 'notified_at': now_ts}
            else:
                next_active[key] = {'message': msg, 'notified_at': last_notified}

    for key, old in prev_active.items():
        if key not in new_alerts:
            recovered[key] = old.get('message', key)

    return notify_alerts, recovered, next_active


def format_patrol_alert(notify_alerts, recovered, metrics):
    """拼装发给机主的异常告警或恢复通知正文。"""
    parts = []
    if notify_alerts:
        lines = ['🚨 主机定时巡检发现异常']
        for msg in notify_alerts.values():
            lines.append('• ' + msg)
        lines.append('当前概况：内存 ' + format(float(metrics.get('mem_pct', 0)), '.1f')
                     + '% · 磁盘 ' + format(float(metrics.get('disk_pct', 0)), '.1f')
                     + '% · 负载 ' + str(metrics.get('load', '未知')))
        parts.append('\n'.join(lines))
    if recovered:
        lines = ['✅ 以下巡检异常已恢复正常']
        for msg in recovered.values():
            lines.append('• 已恢复：' + msg)
        parts.append('\n'.join(lines))
    return '\n\n'.join(parts)


def format_patrol_panel(config, state):
    """生成「巡检配置」交互面板的可读说明。"""
    status_label = '已开启（静默守卫中，仅异常或恢复时通知）' if config.get('enabled') else '已关闭（不会后台自动检查或推送）'
    last_run = float(state.get('last_run') or 0.0)
    last_time = time.strftime('%m-%d %H:%M:%S', time.localtime(last_run)) if last_run else '尚无记录'
    active_count = len(state.get('active') or {})
    return ('🩺 后台定时巡检与告警设置\n'
            '────────────────\n'
            '当前状态：' + status_label + '\n'
            '巡检周期：每 ' + str(config.get('interval_minutes', 30)) + ' 分钟检查一次\n'
            '告警阈值：磁盘 ≥ ' + str(config.get('disk_threshold', 85)) + '% / 内存 ≥ ' + str(config.get('mem_threshold', 90)) + '%\n'
            '检查项目：根分区磁盘、物理内存、托管服务健康、外部平台凭据到期预警\n'
            '上次巡检：' + last_time + '（' + str(state.get('last_status') or '未执行')
            + (' · 当前异常 ' + str(active_count) + ' 项' if active_count else '') + '）')


def patrol_panel_buttons(config):
    """返回「巡检配置」页面的交互操作按钮清单。"""
    toggle = ('⏸ 关闭定时巡检', '关闭定时巡检') if config.get('enabled') else ('▶️ 开启定时巡检', '开启定时巡检')
    return [
        toggle,
        ('🔎 立即巡检一次', '立即巡检一次'),
        ('⏱ 周期 15分钟', '巡检周期 15分钟'),
        ('⏱ 周期 30分钟', '巡检周期 30分钟'),
        ('⏱ 周期 60分钟', '巡检周期 60分钟'),
        ('💾 磁盘阈值 80%', '磁盘告警阈值 80%'),
        ('💾 磁盘阈值 85%', '磁盘告警阈值 85%'),
        ('💾 磁盘阈值 90%', '磁盘告警阈值 90%'),
    ]
