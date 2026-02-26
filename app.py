#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书智能考勤机器人 - 带管理后台版本
为汽车自媒体团队打造的轻量级考勤解决方案
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify, render_template_string, redirect, url_for
import requests
from functools import wraps

# ==================== 配置部分 ====================

# 环境变量配置（飞书Webhook地址直接配置在这里）
FEISHU_WEBHOOK_URL = os.environ.get('FEISHU_WEBHOOK_URL', 'https://open.feishu.cn/open-apis/bot/v2/hook/213d85e7-868c-408b-aa57-612727239426')
FEISHU_APP_ID = os.environ.get('FEISHU_APP_ID', '')
FEISHU_APP_SECRET = os.environ.get('FEISHU_APP_SECRET', '')
ADMIN_USER_IDS = os.environ.get('ADMIN_USER_IDS', '').split(',')
SCHEDULE_ENABLED = os.environ.get('SCHEDULE_ENABLED', 'true').lower() == 'true'
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')  # 管理后台密码

# 应用配置
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
app.secret_key = os.environ.get('SECRET_KEY', 'attendance-bot-secret-key')
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== 数据库部分 ====================

DATABASE = 'attendance.db'

def init_db():
    """初始化数据库"""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    # 考勤记录表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            user_name TEXT NOT NULL,
            date TEXT NOT NULL,
            check_in_time TEXT,
            check_out_time TEXT,
            morning_status TEXT,
            evening_status TEXT,
            location TEXT,
            task TEXT,
            tasks_json TEXT,
            completion INTEGER DEFAULT 0,
            progress_status TEXT,
            work_summary TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, date)
        )
    ''')

    # 用户配置表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            user_name TEXT NOT NULL,
            is_active INTEGER DEFAULT 1
        )
    ''')

    # 系统配置表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 初始化默认配置
    default_settings = {
        'bot_name': '考勤小助手',
        'welcome_message': '你好！我是考勤小助手',
        'morning_time': '09:00',
        'noon_time': '13:00',
        'evening_time': '18:00',
        'report_time': '20:00',
        'week_report_time': '18:00',
        'month_report_time': '18:00',
        'task_tags': json.dumps(['视频剪辑', '文案撰写', '素材拍摄', '字幕压制', '封面设计', '平台发布']),
        'status_options': json.dumps(['办公室坐班', '外出拍摄', '居家办公', '会议中']),
        'schedule_enabled': 'true',
        'company_location': '',
        'company_lat': '',
        'company_lng': '',
        'checkin_radius': '500'
    }

    for key, value in default_settings.items():
        cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))

    conn.commit()
    conn.close()
    logger.info("数据库初始化完成")

def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def get_setting(key, default=''):
    """获取配置"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cursor.fetchone()
    conn.close()
    return row['value'] if row else default

def set_setting(key, value):
    """设置配置"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)',
                   (key, value, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()

def get_all_settings():
    """获取所有配置"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT key, value FROM settings')
    rows = cursor.fetchall()
    conn.close()
    settings = {}
    for row in rows:
        try:
            # 尝试解析JSON
            settings[row['key']] = json.loads(row['value'])
        except:
            settings[row['key']] = row['value']
    return settings

# ==================== 飞书API部分 ====================

def send_feishu_message(webhook_url, message):
    """发送飞书消息"""
    try:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        response = requests.post(webhook_url, headers=headers, json=message, timeout=10)
        result = response.json()
        return result.get("code") == 0
    except Exception as e:
        logger.error(f"发送消息异常: {e}")
        return False

def send_text_message(text, webhook_url=None):
    """发送文本消息"""
    url = webhook_url or FEISHU_WEBHOOK_URL
    if not url:
        return False
    message = {"msg_type": "text", "text": {"content": text}}
    return send_feishu_message(url, message)

def send_rich_text_message(title, content, webhook_url=None):
    """发送富文本消息"""
    url = webhook_url or FEISHU_WEBHOOK_URL
    if not url:
        return False
    message = {
        "msg_type": "post",
        "post": {
            "zh_cn": {
                "title": title,
                "content": [[[{"tag": "text", "text": content}]]]
            }
        }
    }
    return send_feishu_message(url, message)

# ==================== 考勤业务逻辑 ====================

def get_today_date():
    return date.today().strftime("%Y-%m-%d")

def get_current_time():
    return datetime.now().strftime("%H:%M:%S")

def register_user(user_id, user_name):
    """注册用户"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT OR REPLACE INTO users (user_id, user_name, is_active) VALUES (?, ?, 1)',
                       (user_id, user_name))
        conn.commit()
        return True
    except:
        return False
    finally:
        conn.close()

def check_in(user_id, user_name, status, task, location="办公室", tasks_json="[]"):
    """签到"""
    conn = get_db()
    cursor = conn.cursor()
    today = get_today_date()
    current_time = get_current_time()

    try:
        cursor.execute('SELECT id FROM attendance WHERE user_id = ? AND date = ?', (user_id, today))
        if cursor.fetchone():
            conn.close()
            return False, "您今天已经签到过了！"

        cursor.execute('''
            INSERT INTO attendance (user_id, user_name, date, check_in_time, morning_status, task, location, tasks_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, user_name, today, current_time, status, task, location, tasks_json))
        conn.commit()
        register_user(user_id, user_name)
        return True, f"签到成功！\n状态：{status}\n任务：{task}"
    except Exception as e:
        logger.error(f"签到失败: {e}")
        return False, "签到失败，请重试"
    finally:
        conn.close()

def check_out(user_id, completion, work_summary=''):
    """签退"""
    conn = get_db()
    cursor = conn.cursor()
    today = get_today_date()
    current_time = get_current_time()

    try:
        cursor.execute('''
            UPDATE attendance
            SET check_out_time = ?, completion = ?, evening_status = '已完成工作', work_summary = ?
            WHERE user_id = ? AND date = ?
        ''', (current_time, completion, work_summary, user_id, today))
        conn.commit()

        if cursor.rowcount == 0:
            return False, "您今天还没有签到！"

        return True, f"签退成功！\n今日完成度：{completion}%"
    except Exception as e:
        logger.error(f"签退失败: {e}")
        return False, "签退失败，请重试"
    finally:
        conn.close()

def update_progress(user_id, progress_status):
    """更新进度状态"""
    conn = get_db()
    cursor = conn.cursor()
    today = get_today_date()

    try:
        cursor.execute('UPDATE attendance SET progress_status = ? WHERE user_id = ? AND date = ?',
                       (progress_status, user_id, today))
        conn.commit()
        return True
    finally:
        conn.close()

def get_today_status():
    """获取今日考勤状态"""
    conn = get_db()
    cursor = conn.cursor()
    today = get_today_date()

    try:
        cursor.execute('''
            SELECT user_name, check_in_time, check_out_time, morning_status, evening_status,
                   task, location, completion, progress_status, work_summary, tasks_json
            FROM attendance WHERE date = ? ORDER BY check_in_time
        ''', (today,))

        rows = cursor.fetchall()
        results = []
        for row in rows:
            results.append({
                "name": row[0],
                "check_in": row[1],
                "check_out": row[2],
                "morning_status": row[3],
                "evening_status": row[4],
                "task": row[5],
                "location": row[6],
                "completion": row[7],
                "progress_status": row[8],
                "work_summary": row[9],
                "tasks": json.loads(row[10]) if row[10] else []
            })
        return results
    finally:
        conn.close()

def get_user_status(user_id):
    """获取指定用户今日状态"""
    conn = get_db()
    cursor = conn.cursor()
    today = get_today_date()

    try:
        cursor.execute('''
            SELECT user_name, check_in_time, check_out_time, morning_status, task, completion, progress_status
            FROM attendance WHERE user_id = ? AND date = ?
        ''', (user_id, today))

        row = cursor.fetchone()
        if row:
            return {
                "name": row[0],
                "check_in": row[1],
                "check_out": row[2],
                "status": row[3],
                "task": row[4],
                "completion": row[5],
                "progress_status": row[6]
            }
        return None
    finally:
        conn.close()

def get_all_users():
    """获取所有用户"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT user_id, user_name FROM users WHERE is_active = 1')
        rows = cursor.fetchall()
        return [{"id": row[0], "name": row[1]} for row in rows]
    finally:
        conn.close()

def build_daily_report():
    """构建每日汇报"""
    statuses = get_today_status()
    all_users = get_all_users()
    today = get_today_date()

    checked_in_names = [s['name'] for s in statuses]
    not_checked_in = [u['name'] for u in all_users if u['name'] not in checked_in_names]

    content = f"📊 **今日团队去向** - {today}\n\n"

    if statuses:
        for s in statuses:
            status_icon = {"办公室坐班": "🏢", "外出拍摄": "📹", "居家办公": "💻", "会议中": "📞"}.get(s['morning_status'], "📌")
            task_text = s['task'] if s['task'] else "未填写任务"
            progress_icon = "🟢" if s.get('progress_status') == '一切正常' else "🔴"
            content += f"• {s['name']} {status_icon} {s['morning_status']}\n"
            content += f"  📝 {task_text}\n"
            content += f"  {progress_icon} 进度: {s.get('progress_status', '未确认')}\n"
            if s['check_out']:
                content += f"  ⏰ 已签退 ({s['completion']}%)\n"
            content += "\n"

    if not_checked_in:
        content += "⏰ **未签到**\n"
        for name in not_checked_in:
            content += f"• {name}\n"

    return content

# ==================== 管理后台 ====================

ADMIN_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>考勤机器人管理后台</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f6f7; }
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; }
        .header h1 { font-size: 24px; }
        .nav { background: white; padding: 15px 20px; border-bottom: 1px solid #e5e6e8; }
        .nav a { color: #3370ff; text-decoration: none; margin-right: 20px; padding: 8px 16px; border-radius: 6px; }
        .nav a:hover, .nav a.active { background: #f5f7ff; }
        .container { max-width: 1200px; margin: 20px auto; padding: 0 20px; }
        .card { background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
        .card h2 { font-size: 18px; margin-bottom: 20px; color: #1f2329; }
        .form-group { margin-bottom: 20px; }
        .form-group label { display: block; margin-bottom: 8px; font-weight: 500; color: #1f2329; }
        .form-group input, .form-group select, .form-group textarea { width: 100%; padding: 10px 14px; border: 1px solid #e5e6e8; border-radius: 8px; font-size: 14px; }
        .form-group input:focus, .form-group textarea:focus { outline: none; border-color: #3370ff; }
        .form-group textarea { min-height: 100px; resize: vertical; }
        .form-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }
        .btn { padding: 10px 24px; background: #3370ff; color: white; border: none; border-radius: 8px; font-size: 14px; cursor: pointer; }
        .btn:hover { background: #2960e6; }
        .btn-success { background: #00b365; }
        .btn-success:hover { background: #009a55; }
        .tag-input { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
        .tag { display: inline-flex; align-items: center; padding: 6px 12px; background: #f0f1f3; border-radius: 16px; font-size: 13px; }
        .tag .remove { margin-left: 8px; cursor: pointer; color: #ff4d4f; }
        .tag-input input { flex: 1; min-width: 120px; }
        .alert { padding: 12px 16px; background: #e8f9f0; color: #00b365; border-radius: 8px; margin-bottom: 20px; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #f0f1f3; }
        th { background: #f5f6f7; font-weight: 500; color: #5e6e82; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🚗 考勤机器人管理后台</h1>
    </div>
    <div class="nav">
        <a href="/" class="{{ 'active' if page == 'settings' else '' }}">基本设置</a>
        <a href="/timing" class="{{ 'active' if page == 'timing' else '' }}">定时任务</a>
        <a href="/tasks" class="{{ 'active' if page == 'tasks' else '' }}">任务标签</a>
        <a href="/status" class="{{ 'active' if page == 'status' else '' }}">考勤状态</a>
        <a href="/data" class="{{ 'active' if page == 'data' else '' }}">考勤数据</a>
    </div>
    <div class="container">
        {% if message %}
        <div class="alert">{{ message }}</div>
        {% endif %}

        {% if page == 'settings' %}
        <div class="card">
            <h2>基本设置</h2>
            <form method="post" action="/settings/save">
                <div class="form-group">
                    <label>机器人名称</label>
                    <input type="text" name="bot_name" value="{{ settings.bot_name }}">
                </div>
                <div class="form-group">
                    <label>欢迎语</label>
                    <textarea name="welcome_message">{{ settings.welcome_message }}</textarea>
                </div>
                <div class="form-group">
                    <label>公司地址（用于定位签到）</label>
                    <input type="text" name="company_location" value="{{ settings.company_location }}" placeholder="例如：北京市朝阳区建国路88号">
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label>签到有效半径（米）</label>
                        <input type="number" name="checkin_radius" value="{{ settings.checkin_radius }}">
                    </div>
                    <div class="form-group">
                        <label>开启定时任务</label>
                        <select name="schedule_enabled">
                            <option value="true" {{ 'selected' if settings.schedule_enabled == true else '' }}>开启</option>
                            <option value="false" {{ 'selected' if settings.schedule_enabled == false else '' }}>关闭</option>
                        </select>
                    </div>
                </div>
                <button type="submit" class="btn btn-success">保存设置</button>
            </form>
        </div>
        {% endif %}

        {% if page == 'timing' %}
        <div class="card">
            <h2>定时任务设置</h2>
            <form method="post" action="/timing/save">
                <div class="form-row">
                    <div class="form-group">
                        <label>早安签到提醒时间</label>
                        <input type="time" name="morning_time" value="{{ settings.morning_time }}">
                    </div>
                    <div class="form-group">
                        <label>午间进度确认时间</label>
                        <input type="time" name="noon_time" value="{{ settings.noon_time }}">
                    </div>
                    <div class="form-group">
                        <label>晚间签退提醒时间</label>
                        <input type="time" name="evening_time" value="{{ settings.evening_time }}">
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label>日报发送时间</label>
                        <input type="time" name="report_time" value="{{ settings.report_time }}">
                    </div>
                    <div class="form-group">
                        <label>周报发送时间</label>
                        <input type="time" name="week_report_time" value="{{ settings.week_report_time }}">
                    </div>
                    <div class="form-group">
                        <label>月报发送时间</label>
                        <input type="time" name="month_report_time" value="{{ settings.month_report_time }}">
                    </div>
                </div>
                <button type="submit" class="btn btn-success">保存时间设置</button>
            </form>
        </div>
        {% endif %}

        {% if page == 'tasks' %}
        <div class="card">
            <h2>任务标签管理</h2>
            <form method="post" action="/tasks/save">
                <div class="form-group">
                    <label>当前任务标签（点击删除，或输入新标签添加）</label>
                    <div class="tag-input" id="tagContainer">
                        {% for tag in settings.task_tags %}
                        <span class="tag">{{ tag }}<span class="remove" onclick="removeTag(this, '{{ tag }}')">×</span></span>
                        {% endfor %}
                        <input type="hidden" name="task_tags" id="taskTagsInput" value="{{ settings.task_tags|tojson }}">
                        <input type="text" id="newTag" placeholder="输入新标签后按回车添加" onkeypress="addTag(event)">
                    </div>
                </div>
                <button type="submit" class="btn btn-success">保存任务标签</button>
            </form>
        </div>
        {% endif %}

        {% if page == 'status' %}
        <div class="card">
            <h2>考勤状态管理</h2>
            <form method="post" action="/status/save">
                <div class="form-group">
                    <label>考勤状态选项</label>
                    <div class="tag-input">
                        {% for status in settings.status_options %}
                        <span class="tag">{{ status }}<span class="remove" onclick="removeStatus(this, '{{ status }}')">×</span></span>
                        {% endfor %}
                        <input type="hidden" name="status_options" id="statusInput" value="{{ settings.status_options|tojson }}">
                        <input type="text" id="newStatus" placeholder="输入新状态后按回车添加" onkeypress="addStatus(event)">
                    </div>
                </div>
                <button type="submit" class="btn btn-success">保存状态选项</button>
            </form>
        </div>
        {% endif %}

        {% if page == 'data' %}
        <div class="card">
            <h2>考勤数据查看</h2>
            <p style="color: #8f959e; margin-bottom: 20px;">查看团队考勤记录</p>
            <table>
                <thead>
                    <tr>
                        <th>日期</th>
                        <th>姓名</th>
                        <th>签到时间</th>
                        <th>签退时间</th>
                        <th>状态</th>
                        <th>完成度</th>
                    </tr>
                </thead>
                <tbody>
                    {% for record in records %}
                    <tr>
                        <td>{{ record.date }}</td>
                        <td>{{ record.user_name }}</td>
                        <td>{{ record.check_in_time or '-' }}</td>
                        <td>{{ record.check_out_time or '-' }}</td>
                        <td>{{ record.morning_status or '-' }}</td>
                        <td>{{ record.completion or 0 }}%</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% endif %}
    </div>

    <script>
        let taskTags = {{ settings.task_tags|tojson }};
        let statusOptions = {{ settings.status_options|tojson }};

        function removeTag(el, tag) {
            taskTags = taskTags.filter(t => t !== tag);
            document.getElementById('taskTagsInput').value = JSON.stringify(taskTags);
            el.parentElement.remove();
        }

        function addTag(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                const tag = document.getElementById('newTag').value.trim();
                if (tag && !taskTags.includes(tag)) {
                    taskTags.push(tag);
                    const span = document.createElement('span');
                    span.className = 'tag';
                    span.innerHTML = tag + '<span class="remove" onclick="removeTag(this, \\'' + tag + '\\')">×</span>';
                    document.getElementById('newTag').before(span);
                    document.getElementById('taskTagsInput').value = JSON.stringify(taskTags);
                    document.getElementById('newTag').value = '';
                }
            }
        }

        function removeStatus(el, status) {
            statusOptions = statusOptions.filter(s => s !== status);
            document.getElementById('statusInput').value = JSON.stringify(statusOptions);
            el.parentElement.remove();
        }

        function addStatus(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                const status = document.getElementById('newStatus').value.trim();
                if (status && !statusOptions.includes(status)) {
                    statusOptions.push(status);
                    const span = document.createElement('span');
                    span.className = 'tag';
                    span.innerHTML = status + '<span class="remove" onclick="removeStatus(this, \\'' + status + '\\')">×</span>';
                    document.getElementById('newStatus').before(span);
                    document.getElementById('statusInput').value = JSON.stringify(statusOptions);
                    document.getElementById('newStatus').value = '';
                }
            }
        }
    </script>
</body>
</html>
'''

# ==================== 路由部分 ====================

@app.route('/')
def admin_index():
    """管理后台首页"""
    settings = get_all_settings()
    return render_template_string(ADMIN_HTML, page='settings', settings=settings)

@app.route('/timing')
def admin_timing():
    """定时任务设置"""
    settings = get_all_settings()
    return render_template_string(ADMIN_HTML, page='timing', settings=settings)

@app.route('/tasks')
def admin_tasks():
    """任务标签管理"""
    settings = get_all_settings()
    return render_template_string(ADMIN_HTML, page='tasks', settings=settings)

@app.route('/status')
def admin_status():
    """考勤状态管理"""
    settings = get_all_settings()
    return render_template_string(ADMIN_HTML, page='status', settings=settings)

@app.route('/data')
def admin_data():
    """考勤数据查看"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM attendance ORDER BY date DESC, check_in_time DESC LIMIT 100')
    records = cursor.fetchall()
    conn.close()
    settings = get_all_settings()
    return render_template_string(ADMIN_HTML, page='data', settings=settings, records=records)

@app.route('/settings/save', methods=['POST'])
def save_settings():
    """保存基本设置"""
    set_setting('bot_name', request.form.get('bot_name', '考勤小助手'))
    set_setting('welcome_message', request.form.get('welcome_message', '你好！'))
    set_setting('company_location', request.form.get('company_location', ''))
    set_setting('checkin_radius', request.form.get('checkin_radius', '500'))
    set_setting('schedule_enabled', request.form.get('schedule_enabled', 'true'))
    settings = get_all_settings()
    return render_template_string(ADMIN_HTML, page='settings', settings=settings, message='保存成功！')

@app.route('/timing/save', methods=['POST'])
def save_timing():
    """保存定时设置"""
    set_setting('morning_time', request.form.get('morning_time', '09:00'))
    set_setting('noon_time', request.form.get('noon_time', '13:00'))
    set_setting('evening_time', request.form.get('evening_time', '18:00'))
    set_setting('report_time', request.form.get('report_time', '20:00'))
    set_setting('week_report_time', request.form.get('week_report_time', '18:00'))
    set_setting('month_report_time', request.form.get('month_report_time', '18:00'))
    settings = get_all_settings()
    return render_template_string(ADMIN_HTML, page='timing', settings=settings, message='时间设置已保存！')

@app.route('/tasks/save', methods=['POST'])
def save_tasks():
    """保存任务标签"""
    tags = request.form.get('task_tags', '[]')
    set_setting('task_tags', tags)
    settings = get_all_settings()
    return render_template_string(ADMIN_HTML, page='tasks', settings=settings, message='任务标签已保存！')

@app.route('/status/save', methods=['POST'])
def save_status():
    """保存状态选项"""
    status = request.form.get('status_options', '[]')
    set_setting('status_options', status)
    settings = get_all_settings()
    return render_template_string(ADMIN_HTML, page='status', settings=settings, message='状态选项已保存！')

# ==================== 飞书Webhook接口 ====================

@app.route('/feishu/webhook', methods=['POST'])
def feishu_webhook():
    """接收飞书Webhook消息"""
    try:
        data = request.get_json()
        logger.info(f"收到飞书消息: {data}")

        if not data or data.get('msg_type') != 'text':
            return jsonify({"code": 0, "message": "ok"})

        user_id = data.get('sender', {}).get('user_id', '')
        user_name = data.get('sender', {}).get('sender_id', {}).get('name', '未知用户')
        text_content = data.get('text', {}).get('content', '').strip()

        register_user(user_id, user_name)

        settings = get_all_settings()
        task_tags = settings.get('task_tags', ['视频剪辑', '文案撰写', '素材拍摄'])

        if text_content in ['签到', '/checkin', '/签到']:
            # 构建签到卡片
            task_buttons = ''.join([f'<button type="button" onclick="selectTask(this)">{t}</button>' for t in task_tags[:6]])
            card = {
                "header": {"title": {"tag": "plain_text", "content": "☀️ 早安！请签到"}, "template": "blue"},
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"📍 当前定位：{settings.get('company_location', '公司地址未设置')}\n选择您的状态："}},
                    {"tag": "action", "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "🏢 办公室坐班"}, "type": "primary", "value": {"action": "checkin", "status": "办公室坐班"}},
                        {"tag": "button", "text": {"tag": "plain_text", "content": "📹 外出拍摄"}, "type": "primary", "value": {"action": "checkin", "status": "外出拍摄"}}
                    ]},
                    {"tag": "action", "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "💻 居家办公"}, "type": "primary", "value": {"action": "checkin", "status": "居家办公"}},
                        {"tag": "button", "text": {"tag": "plain_text", "content": "📞 会议中"}, "type": "primary", "value": {"action": "checkin", "status": "会议中"}}
                    ]}
                ]
            }
            send_feishu_message(FEISHU_WEBHOOK_URL, {"msg_type": "interactive", "card": json.dumps(card)})
            return jsonify({"code": 0, "message": "ok"})

        elif text_content in ['签退', '/checkout', '/签退']:
            card = {
                "header": {"title": {"tag": "plain_text", "content": "🌙 辛苦了！请签退"}, "template": "green"},
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": "请选择完成度："}},
                    {"tag": "action", "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "25% 🔴"}, "value": {"action": "checkout", "completion": 25}},
                        {"tag": "button", "text": {"tag": "plain_text", "content": "50% 🟡"}, "value": {"action": "checkout", "completion": 50}}
                    ]},
                    {"tag": "action", "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "75% 🟢"}, "value": {"action": "checkout", "completion": 75}},
                        {"tag": "button", "text": {"tag": "plain_text", "content": "100% ⭐"}, "type": "primary", "value": {"action": "checkout", "completion": 100}}
                    ]}
                ]
            }
            send_feishu_message(FEISHU_WEBHOOK_URL, {"msg_type": "interactive", "card": json.dumps(card)})
            return jsonify({"code": 0, "message": "ok"})

        elif text_content in ['日报', '/report', '/日报']:
            content = build_daily_report()
            send_rich_text_message("📊 今日团队去向", content)
            return jsonify({"code": 0, "message": "ok"})

        elif text_content in ['帮助', '/help']:
            help_text = f"""🚗 **{settings.get('bot_name', '考勤小助手')}帮助**

*可用命令：*
• 签到 - 每日签到
• 签退 - 每日签退
• 日报 - 查看今日汇总
• 帮助 - 查看帮助信息

*考勤状态：*
🏢 办公室坐班
📹 外出拍摄
💻 居家办公
📞 会议中"""
            send_text_message(help_text)
            return jsonify({"code": 0, "message": "ok"})

        send_text_message(f"收到消息：{text_content}\n\n发送「帮助」查看可用命令")
        return jsonify({"code": 0, "message": "ok"})

    except Exception as e:
        logger.error(f"处理消息失败: {e}")
        return jsonify({"code": 500, "message": "internal error"})

@app.route('/feishu/callback', methods=['POST'])
def feishu_callback():
    """接收飞书卡片回调"""
    try:
        data = request.get_json()
        logger.info(f"收到回调: {data}")

        if data.get('type') != 'interactive':
            return jsonify({"code": 0, "message": "ok"})

        action = data.get('action', {})
        action_value = action.get('value', {})
        user_id = data.get('operator', {}).get('user_id', '')
        user_name = data.get('operator', {}).get('name', '未知用户')

        if action_value.get('action') == 'checkin':
            status = action_value.get('status', '办公室坐班')
            success, msg = check_in(user_id, user_name, status, "日常工作", location=status)
            send_text_message(f"@{user_name} {msg}")

        elif action_value.get('action') == 'checkout':
            completion = action_value.get('completion', 0)
            success, msg = check_out(user_id, completion)
            send_text_message(f"@{user_name} {msg}")

        return jsonify({"code": 0, "message": "ok"})

    except Exception as e:
        logger.error(f"处理回调失败: {e}")
        return jsonify({"code": 500, "message": "internal error"})

# ==================== 健康检查 ====================

@app.route('/health')
def health():
    """健康检查"""
    return jsonify({
        "status": "ok",
        "message": "飞书考勤机器人运行中",
        "admin_url": "/",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

# ==================== 主程序 ====================

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

