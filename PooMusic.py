#!/usr/bin/env python3
# coding=utf-8

# Depends: gstreamer1.0-plugins-good  gstreamer1.0-plugins-ugly gir1.2-gst-plugins-base-1.0 gir1.2-gstreamer-1.0 libgssdp-1.6-0 libgstreamer-plugins-bad1.0-0 libgupnp-1.6-0 libgupnp-igd-1.6-0 ibnice10 libva-drm2 libva2 python3-gst-1.0 python3-typing-extensions papirus-icon-theme

import gi
import random
gi.require_version('Gtk', '3.0')
gi.require_version('Gst', '1.0')
from gi.repository import Gtk, Gst, GLib, GObject
import os
import re
import pathlib
import threading

# 初始化GStreamer
Gst.init(None)

# 定义音乐文件夹路径（适配不同系统的"音乐"文件夹）
if os.name == 'nt':  # Windows系统
    MUSIC_DIR = os.path.join(os.environ['USERPROFILE'], '音乐')
else:  # Linux/Mac系统（中文环境）
    MUSIC_DIR = os.path.expanduser("~/音乐")
    # 备用路径（如果"音乐"文件夹不存在，尝试Music）
    if not os.path.exists(MUSIC_DIR):
        MUSIC_DIR = os.path.expanduser("~/Music")

# 配置项：可选择是否快速加载（跳过时长获取）
FAST_LOAD = True  # True=快速加载（无时长），False=精确时长（稍慢）

class LrcParser:
    """增强版LRC歌词解析器"""
    def __init__(self, path=''):
        self.lrc_list = []  # 格式: [(时间戳, 歌词文本), ...]
        if path and os.path.exists(path):
            self.load(path)

    def parse_time(self, ts):
        """解析时间戳为秒数"""
        try:
            if '.' in ts:
                m, s = ts.split(':')
                s, ms = s.split('.')
                return float(m)*60 + float(s) + float(ms)/100
            else:
                m, s = ts.split(':')
                return float(m)*60 + float(s)
        except:
            return 0

    def load(self, path):
        """加载并解析LRC文件"""
        self.lrc_list = []
        try:
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    # 匹配多时间戳格式（如 [01:23.45][02:34.56]歌词）
                    time_matches = re.findall(r'\[(\d+:\d+\.?\d*)\]', line)
                    if time_matches:
                        text = re.sub(r'\[\d+:\d+\.?\d*\]', '', line).strip()
                        if not text:
                            continue
                        for ts in time_matches:
                            t = self.parse_time(ts)
                            self.lrc_list.append((t, text))
            # 去重并按时间排序
            self.lrc_list = list(dict.fromkeys(self.lrc_list))
            self.lrc_list.sort(key=lambda x:x[0])
        except Exception as e:
            print(f"解析歌词失败: {e}")

    def get_current_line_index(self, pos):
        """获取当前进度对应的歌词行索引"""
        current_idx = 0
        for i, (t, txt) in enumerate(self.lrc_list):
            if t <= pos:
                current_idx = i
            else:
                break
        return current_idx if self.lrc_list else -1

class MusicPlayer(Gtk.Window):
    def __init__(self):
        super().__init__(title='铺音乐播放器')
        self.set_default_size(1000, 600)
        self.set_border_width(10)  # 关键修改：窗口内边距设为10

        # 核心状态
        self.playlist = []          # 播放列表 [(文件路径, 歌曲名, 时长秒数), ...]
        self.current_song_idx = -1  # 当前播放歌曲索引
        self.play_flag = False      # 播放状态
        self.curr_pos = 0.0         # 当前播放进度
        self.lrc = LrcParser()      # 歌词解析器
        self.current_duration = 0.0 # 当前歌曲时长（秒）
        self.loading_thread = None  # 加载歌曲的线程
        
        # 播放模式：0-顺序 1-循环 2-单曲循环 3-随机
        self.play_mode = 0
        self.mode_labels = [{'顺序播放':'media-playlist-normal'}, {'循环播放':'media-playlist-repeat'}, {'单曲循环':'media-playlist-repeat-song'}, {'随机播放':'media-playlist-shuffle'}]
        # 新增：播放模式按钮引用
        self.mode_buttons = []
        
        # 随机播放相关：保存原始列表和随机索引
        self.original_playlist = []  # 保存原始播放列表
        self.random_playlist = []    # 随机播放列表
        self.random_index = -1       # 随机播放当前索引

        # GStreamer 播放器
        self.player = Gst.ElementFactory.make('playbin', 'player')
        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect('message::eos', self.on_eos)       # 播放结束
        bus.connect('message::error', self.on_error)   # 播放错误

        # 歌词相关
        self.lrc_listbox = None      # 歌词列表组件
        self.lrc_labels = []         # 歌词标签缓存
        self.current_lrc_index = -1  # 当前高亮歌词索引

        # 新增：当前播放歌曲显示标签
        self.current_song_label = None
        
        # 构建UI
        self.build_ui()

        # 异步加载音乐文件夹歌曲（不阻塞UI）
        self.load_music_folder_async()

        # 定时器刷新UI（300ms一次）
        GLib.timeout_add(300, self.update_ui)

    def build_ui(self):
        """构建完整UI布局"""
        # 主布局：外层垂直布局（顶部播放状态 + 主体内容）
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)  # 无间距
        main_vbox.set_border_width(0)  # 无内边距
        self.add(main_vbox)
        
        # 主体内容：播放列表(左) + 右侧内容(右) - 关键：先放主体，再放顶部播放状态
        content_hbox = Gtk.Box(spacing=8)  # 仅左右间距
        content_hbox.set_border_width(0)
        main_vbox.pack_start(content_hbox, True, True, 0)

        # 1. 左侧播放列表区域 - 完全置顶
        self.build_playlist_area(content_hbox)

        # 2. 右侧内容容器（当前播放 + 控制栏 + 歌词区）
        right_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        right_vbox.set_size_request(740, -1)  # 固定宽度（1000-250-8=742，取740）
        right_vbox.set_border_width(0)
        content_hbox.pack_start(right_vbox, True, True, 0)

        # 2.1 顶部当前播放歌曲显示区域 - 宽度适配歌词区
        self.build_current_playing_area(right_vbox)

        # 2.2 右侧主内容区域（控制栏 + 歌词区）
        self.build_main_content_area(right_vbox)

    def build_current_playing_area(self, parent):
        """构建顶部当前播放歌曲显示区域 - 宽度适配歌词区"""
        playing_box = Gtk.Box(spacing=5)
        playing_box.set_border_width(0)
        playing_box.set_halign(Gtk.Align.START)  # 左对齐
        playing_box.set_size_request(-1, 40)     # 固定高度
        
        # 标题标签
        title_label = Gtk.Label()
        title_label.set_markup('<span weight="bold" size="large">当前播放：</span>')
        title_label.set_xalign(1.0)  # 右对齐
        title_label.set_size_request(300, -1)
        playing_box.pack_start(title_label, False, False, 0)
        
        # 当前歌曲显示标签（核心）
        self.current_song_label = Gtk.Label()
        self.current_song_label.set_markup('<span size="large" color="#e63946">未播放任何歌曲</span>')
        self.current_song_label.set_xalign(0.0)  # 左对齐
        self.current_song_label.set_ellipsize(3)  # 文本过长时省略
        self.current_song_label.set_size_request(350, -1)  # 限制宽度，适配歌词区
        playing_box.pack_start(self.current_song_label, False, False, 0)
        
        parent.pack_start(playing_box, False, False, 0)

    def build_playlist_area(self, parent):
        """构建左侧播放列表 - 完全置顶，无任何顶部空白"""
        # 播放列表容器 - 无任何间距和内边距
        playlist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        playlist_box.set_size_request(250, -1)  # 固定宽度
        playlist_box.set_border_width(0)
        playlist_box.set_vexpand(True)  # 垂直扩展填满空间
        parent.pack_start(playlist_box, False, False, 0)

        # 播放列表标题 + 加载状态 - 无间距
        title_box = Gtk.Box(spacing=3)
        playlist_title = Gtk.Label()
        playlist_title.set_markup('<span weight="bold" size="xx-large" color="#e63946">铺音乐</span>')
        playlist_title.set_margin_top(0)
        playlist_title.set_margin_bottom(0)
        playlist_title.set_size_request(-1, 40)  # 限制高度，适配歌词区
        
        self.loading_label = Gtk.Label()
        self.loading_label.set_markup('<span size="small" color="#666666">加载中...</span>')
        self.loading_label.set_margin_top(0)
        self.loading_label.set_margin_bottom(0)
        
        title_box.pack_start(playlist_title, True, True, 0)
        title_box.pack_start(self.loading_label, False, False, 0)
        playlist_box.pack_start(title_box, False, False, 0)

        # 播放列表控制按钮 - 无间距
        playlist_ctrl = Gtk.Box(spacing=3)
        btn_add = Gtk.Button()
        btn_add.set_tooltip_text("添加歌曲")
        icon = Gtk.Image.new_from_icon_name("media-track-add-amarok", Gtk.IconSize.BUTTON)
        btn_add.set_image(icon)
        btn_add.set_always_show_image(True)  # 让图标居中显示

        btn_remove = Gtk.Button()
        btn_remove.set_tooltip_text("删除歌曲")
        icon = Gtk.Image.new_from_icon_name("media-track-remove-amarok", Gtk.IconSize.BUTTON)
        btn_remove.set_image(icon)
        btn_remove.set_always_show_image(True)  # 让图标居中显示

        btn_clear = Gtk.Button()
        btn_clear.set_tooltip_text("清空列表")
        icon = Gtk.Image.new_from_icon_name("rabbitvcs-clear", Gtk.IconSize.BUTTON)
        btn_clear.set_image(icon)
        btn_clear.set_always_show_image(True)  # 让图标居中显示

        btn_add.set_border_width(4)
        btn_remove.set_border_width(4)
        btn_clear.set_border_width(4)
        
        btn_add.connect('clicked', self.on_add_song)
        btn_remove.connect('clicked', self.on_remove_song)
        btn_clear.connect('clicked', self.on_clear_playlist)
        playlist_ctrl.pack_start(btn_add, True, True, 0)
        playlist_ctrl.pack_start(btn_remove, True, True, 0)
        playlist_ctrl.pack_start(btn_clear, True, True, 0)
        playlist_box.pack_start(playlist_ctrl, False, False, 0)

        # 播放列表滚动窗口 - 占满剩余空间，无空白
        scrolled_playlist = Gtk.ScrolledWindow()
        scrolled_playlist.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled_playlist.set_border_width(0)
        scrolled_playlist.set_vexpand(True)  # 垂直扩展
        playlist_box.pack_start(scrolled_playlist, True, True, 0)

        # 播放列表TreeView
        self.playlist_store = Gtk.ListStore(str, str, float)
        self.playlist_view = Gtk.TreeView(model=self.playlist_store)
        self.playlist_view.set_border_width(0)
        
        # 移除选中背景样式
        self.playlist_view.set_can_focus(False)
        self.playlist_view.set_hover_selection(False)
        style_provider = Gtk.CssProvider()
        css = """
        GtkTreeView {
            background-color: transparent;
        }
        GtkTreeView:selected {
            background-color: transparent;
            color: inherit;
        }
        GtkTreeView row:selected {
            background-color: transparent;
            color: inherit;
        }
        """
        style_provider.load_from_data(css.encode('utf-8'))
        self.playlist_view.get_style_context().add_provider(
            style_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # 自定义单元格渲染器
        renderer_name = Gtk.CellRendererText()
        def cell_data_func(column, cell, model, iter_, data):
            path = model.get_path(iter_)
            row_idx = path[0]
            if row_idx == self.current_song_idx and self.play_flag:
                cell.set_property('markup', f'<span color="#e63946" weight="bold" size="large">{model[iter_][1]}</span>')
            else:
                cell.set_property('text', model[iter_][1])
        
        column_name = Gtk.TreeViewColumn('播放列表', renderer_name)
        column_name.set_cell_data_func(renderer_name, cell_data_func)
        column_name.set_expand(True)
        self.playlist_view.append_column(column_name)

        # 播放模式按钮区域 - 无间距
        mode_box = Gtk.Box(spacing=1)
        # 创建三个播放模式按钮
        for i, tooltip in enumerate(self.mode_labels):
            btn = Gtk.Button()
            (key, value), = tooltip.items()
            btn.set_tooltip_text(key)
            icon = Gtk.Image.new_from_icon_name(value, Gtk.IconSize.BUTTON)
            btn.set_image(icon)
            btn.set_always_show_image(True)  # 让图标居中显示
            btn.connect('clicked', self.on_mode_button_click, i)
            btn.set_border_width(2)
            self.mode_buttons.append(btn)
            mode_box.pack_start(btn, True, True, 0)
        playlist_box.pack_start(mode_box, False, False, 0)

        # 初始化选中状态
        self.update_mode_buttons_style()

        # 点击事件
        self.playlist_view.get_selection().set_mode(Gtk.SelectionMode.NONE)
        self.playlist_view.connect('button-press-event', self.on_playlist_click_new)
        
        scrolled_playlist.add(self.playlist_view)

    def build_main_content_area(self, parent):
        """构建右侧主内容（控制栏 + 歌词区）"""
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        main_vbox.set_border_width(0)
        parent.pack_start(main_vbox, True, True, 0)

        # 顶部控制栏
        hbox_ctrl = Gtk.Box(spacing=5)
        
        # 播放控制按钮
        self.btn_prev = Gtk.Button()
        self.btn_prev.set_tooltip_text("上一曲")
        icon = Gtk.Image.new_from_icon_name("media-skip-backward", Gtk.IconSize.BUTTON)
        self.btn_prev.set_image(icon)
        self.btn_prev.set_always_show_image(True)  # 让图标居中显示

        self.btn_play = Gtk.Button()
        self.btn_play.set_tooltip_text("播放")
        icon = Gtk.Image.new_from_icon_name("media-playback-start", Gtk.IconSize.BUTTON)
        self.btn_play.set_image(icon)
        self.btn_play.set_always_show_image(True)  # 让图标居中显示

        self.btn_next = Gtk.Button()
        self.btn_next.set_tooltip_text("下一曲")
        icon = Gtk.Image.new_from_icon_name("media-skip-forward", Gtk.IconSize.BUTTON)
        self.btn_next.set_image(icon)
        self.btn_next.set_always_show_image(True)  # 让图标居中显示

        self.btn_stop = Gtk.Button()
        self.btn_stop.set_tooltip_text("停止")
        icon = Gtk.Image.new_from_icon_name("media-playback-stop", Gtk.IconSize.BUTTON)
        self.btn_stop.set_image(icon)
        self.btn_stop.set_always_show_image(True)  # 让图标居中显示

        self.btn_prev.set_border_width(4)
        self.btn_play.set_border_width(4)
        self.btn_next.set_border_width(4)
        self.btn_stop.set_border_width(4)
        
        # 进度条容器
        progress_box = Gtk.Box(spacing=5)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale.set_draw_value(False)
        
        # 时长显示标签
        self.label_duration = Gtk.Label(label='--:-- / --:--')  # 初始占位符
        
        # 组装进度条区域
        progress_box.pack_start(self.scale, True, True, 0)
        progress_box.pack_start(self.label_duration, False, False, 0)

        # 按钮信号连接
        self.btn_prev.connect('clicked', self.on_prev_song)
        self.btn_play.connect('clicked', self.on_play)
        self.btn_next.connect('clicked', self.on_next_song)
        self.btn_stop.connect('clicked', self.on_stop)
        self.scale.connect('button-release-event', self.on_seek)

        # 组装控制栏
        hbox_ctrl.pack_start(self.btn_prev, False, False, 0)
        hbox_ctrl.pack_start(self.btn_play, False, False, 0)
        hbox_ctrl.pack_start(self.btn_next, False, False, 0)
        hbox_ctrl.pack_start(self.btn_stop, False, False, 0)
        hbox_ctrl.pack_start(progress_box, True, True, 0)
        main_vbox.pack_start(hbox_ctrl, False, False, 0)

        # 歌词滚动显示区域
        self.build_lrc_area(main_vbox)

    def build_lrc_area(self, parent):
        """构建歌词显示区域"""
        # 歌词滚动窗口
        self.scrolled_lrc = Gtk.ScrolledWindow()
        self.scrolled_lrc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled_lrc.set_border_width(0)
        parent.pack_start(self.scrolled_lrc, True, True, 0)

        # 歌词列表ListBox
        self.lrc_listbox = Gtk.ListBox()
        self.lrc_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.scrolled_lrc.add(self.lrc_listbox)

        # 初始歌词提示
        self.reset_lrc_display()

    # 播放模式按钮点击事件
    def on_mode_button_click(self, widget, mode_idx):
        """切换播放模式（按钮点击）"""
        self.play_mode = mode_idx
        self.update_mode_buttons_style()
        
        # 随机播放模式初始化
        if mode_idx == 3:
            self.original_playlist = self.playlist.copy()
            self.random_playlist = self.playlist.copy()
            random.shuffle(self.random_playlist)
            self.random_index = self.current_song_idx if self.current_song_idx != -1 else 0
        else:
            # 退出随机模式时恢复原始列表
            self.random_playlist = []
            self.random_index = -1
            
        print(f"切换播放模式: {self.mode_labels[self.play_mode]}")

    # 更新播放模式按钮样式
    def update_mode_buttons_style(self):
        """更新播放模式按钮的选中高亮样式"""
        for i, btn in enumerate(self.mode_buttons):
            ctx = btn.get_style_context()
            ctx.remove_class('suggested-action')
            ctx.remove_class('active')
            if i == self.play_mode:
                ctx.add_class('suggested-action')
                style_provider = Gtk.CssProvider()
                css = """
                .suggested-action {
                    background-color: #e63946;
                    color: white;
                    font-weight: bold;
                }
                """
                style_provider.load_from_data(css.encode('utf-8'))
                ctx.add_provider(style_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # 播放列表点击事件
    def on_playlist_click_new(self, widget, event):
        """播放列表鼠标点击事件"""
        if event.button == 1:
            x, y = int(event.x), int(event.y)
            path_info = widget.get_path_at_pos(x, y)
            if path_info:
                path, col, cellx, celly = path_info
                idx = path[0]
                self.load_song(idx)
                self.play_flag = True
                self.btn_play.set_tooltip_text("暂停")
                icon = Gtk.Image.new_from_icon_name("media-playback-pause", Gtk.IconSize.BUTTON)
                self.btn_play.set_image(icon)
                # 延迟启动播放，等待时长加载
                GLib.idle_add(self.delayed_play)
                self.update_current_song_display()

    def delayed_play(self):
        """延迟播放，确保时长先加载完成"""
        self.player.set_state(Gst.State.PLAYING)

    def reset_lrc_display(self):
        """重置歌词显示"""
        for child in self.lrc_listbox.get_children():
            self.lrc_listbox.remove(child)
        self.lrc_labels.clear()
        init_label = Gtk.Label()
        init_label.set_markup('<span size="xx-large" weight="bold">未加载歌词</span>')
        self.lrc_listbox.add(init_label)
        self.lrc_labels.append(init_label)
        self.lrc_listbox.show_all()

    def format_time(self, seconds):
        """将秒数格式化为 mm:ss 字符串（增加边界校验）"""
        try:
            # 过滤负数和无效值
            if not isinstance(seconds, (int, float)) or seconds < 0 or seconds > 3600*24:
                return "--:--"
            minutes = int(seconds // 60)
            seconds = int(seconds % 60)
            return f"{minutes:02d}:{seconds:02d}"
        except:
            return "--:--"

    def get_song_duration_fast(self, file_path):
        """快速获取时长（优化异常处理）"""
        if FAST_LOAD:
            return 0.0
        try:
            temp_player = Gst.ElementFactory.make('playbin', 'temp_player')
            temp_player.set_property('uri', f'file://{file_path}')
            timeout = 1
            start = GLib.get_monotonic_time() / 1000000
            while temp_player.get_state(0.1)[1] != Gst.State.READY:
                if GLib.get_monotonic_time() / 1000000 - start > timeout:
                    temp_player.set_state(Gst.State.NULL)
                    return 0.0
                GLib.usleep(50000)
            dur = temp_player.query_duration(Gst.Format.TIME)[1] / Gst.SECOND
            temp_player.set_state(Gst.State.NULL)
            return max(0, dur)  # 确保时长非负
        except:
            return 0.0

    def add_song_to_playlist(self, song_info):
        """线程安全的添加歌曲到播放列表"""
        file_path, song_name, duration_sec = song_info
        # 确保时长非负
        duration_sec = max(0, duration_sec)
        self.playlist.append((file_path, song_name, duration_sec))
        self.playlist_store.append([file_path, song_name, duration_sec])
        if len(self.playlist) == 1:
            self.current_song_idx = 0
            self.load_song(0, auto_play=False)
            self.update_current_song_display()

    def load_music_folder_background(self):
        """后台线程加载歌曲"""
        audio_extensions = ['.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', 'wma']
        song_count = 0
        if os.path.exists(MUSIC_DIR):
            audio_files = []
            for file_path in pathlib.Path(MUSIC_DIR).rglob('*'):
                if file_path.suffix.lower() in audio_extensions:
                    audio_files.append(str(file_path))
            for file_path in audio_files:
                song_name = os.path.splitext(os.path.basename(file_path))[0]
                duration_sec = self.get_song_duration_fast(file_path)
                GLib.idle_add(self.add_song_to_playlist, (file_path, song_name, duration_sec))
                song_count += 1
        GLib.idle_add(self.loading_label.set_markup, 
                     f'<span size="small" color="#666666">已加载 {song_count} 首</span>')
        print(f"加载完成：共 {song_count} 首歌曲")

    def load_music_folder_async(self):
        """异步加载音乐文件夹"""
        self.loading_thread = threading.Thread(target=self.load_music_folder_background)
        self.loading_thread.daemon = True
        self.loading_thread.start()

    def lazy_load_duration(self, file_path):
        """播放时懒加载时长（优化异常处理）"""
        if not FAST_LOAD:
            return 0.0
        try:
            # 先检查是否能获取时长
            success, dur_ns = self.player.query_duration(Gst.Format.TIME)
            if not success:
                return 0.0
            dur = dur_ns / Gst.SECOND
            dur = max(0, dur)  # 确保非负
            for i, (path, name, _) in enumerate(self.playlist):
                if path == file_path:
                    self.playlist[i] = (path, name, dur)
                    break
            return dur
        except:
            return 0.0

    def update_lrc_display(self):
        """更新歌词显示"""
        if not self.lrc.lrc_list:
            self.reset_lrc_display()
            return
        for child in self.lrc_listbox.get_children():
            self.lrc_listbox.remove(child)
        self.lrc_labels.clear()
        for (t, txt) in self.lrc.lrc_list:
            label = Gtk.Label()
            label.set_markup(f'<span size="large">{txt}</span>')
            label.set_halign(0.5)
            label.set_valign(0.5)
            label.set_margin_top(5)
            label.set_margin_bottom(5)
            self.lrc_listbox.add(label)
            self.lrc_labels.append(label)
        self.lrc_listbox.show_all()

    def highlight_current_lrc(self, index):
        """高亮当前歌词"""
        if index < 0 or not self.lrc.lrc_list or index >= len(self.lrc_labels):
            return
        for i, label in enumerate(self.lrc_labels):
            if i == index:
                label.set_markup(f'<span size="xx-large" weight="bold" color="#e63946">{self.lrc.lrc_list[i][1]}</span>')
            else:
                label.set_markup(f'<span size="large" color="#333333">{self.lrc.lrc_list[i][1]}</span>')
        adj = self.scrolled_lrc.get_vadjustment()
        if adj and len(self.lrc_listbox.get_children()) > index:
            list_height = self.lrc_listbox.get_allocated_height()
            visible_height = self.scrolled_lrc.get_allocated_height()
            row_height = list_height / len(self.lrc_listbox.get_children()) if len(self.lrc_listbox.get_children()) > 0 else 30
            target_pos = index * row_height - (visible_height / 2 - row_height / 2)
            target_pos = max(0, min(target_pos, adj.get_upper() - visible_height))
            adj.set_value(target_pos)

    def update_current_song_display(self):
        """更新顶部当前播放歌曲标签"""
        if self.current_song_idx >= 0 and self.current_song_idx < len(self.playlist):
            song_name = self.playlist[self.current_song_idx][1]
            if self.play_flag:
                self.current_song_label.set_markup(f'<span size="large" color="#e63946" weight="bold">▶ {song_name}</span>')
            else:
                self.current_song_label.set_markup(f'<span size="large" color="#666666">⏸ {song_name}</span>')
        else:
            self.current_song_label.set_markup('<span size="large" color="#666666">未播放任何歌曲</span>')
        if self.playlist_view:
            self.playlist_view.queue_draw()

    def load_song(self, idx, auto_play=True):
        """加载指定索引的歌曲（优化时长显示）"""
        if idx < 0 or idx >= len(self.playlist):
            return
        self.player.set_state(Gst.State.READY)
        self.current_song_idx = idx
        song_path, song_name, duration_sec = self.playlist[idx]

        # 先设置占位符，避免0/负数显示
        self.label_duration.set_label('--:-- / --:--')
        self.scale.set_value(0)
        self.curr_pos = 0.0
        self.current_lrc_index = -1
        
        # 设置播放文件
        self.player.set_property('uri', f'file://{song_path}')
        
        # 懒加载时长并更新显示
        if FAST_LOAD and duration_sec == 0.0:
            # 异步加载时长，避免阻塞UI
            GLib.idle_add(self.load_duration_and_update, song_path, idx)
        else:
            self.current_duration = max(0, duration_sec)
            self.label_duration.set_label(f"00:00 / {self.format_time(self.current_duration)}")
        
        # 加载歌词
        base = os.path.splitext(song_path)[0]
        lrc_path = None
        for ext in ['.lrc', '.LRC']:
            lp = base + ext
            if os.path.exists(lp):
                lrc_path = lp
                break
        self.lrc = LrcParser(lrc_path)
        self.update_lrc_display()
        
        # 自动播放逻辑
        if auto_play and self.play_flag:
            # 延迟播放，等待时长加载
            GLib.idle_add(self.delayed_play)
        
        self.update_current_song_display()

    def load_duration_and_update(self, song_path, idx):
        """异步加载时长并更新显示"""
        duration_sec = self.lazy_load_duration(song_path)
        self.current_duration = max(0, duration_sec)
        self.playlist[idx] = (song_path, self.playlist[idx][1], duration_sec)
        self.label_duration.set_label(f"00:00 / {self.format_time(self.current_duration)}")

    def on_add_song(self, widget):
        """手动添加歌曲到播放列表"""
        dlg = Gtk.FileChooserDialog(
            title='选择音频文件', 
            parent=None, 
            action=Gtk.FileChooserAction.OPEN
        )

        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,  # 取消按钮（系统默认样式）
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK         # 打开按钮（系统默认样式）
        )

        dlg.set_select_multiple(True)
        filt = Gtk.FileFilter()
        filt.set_name('音频文件')
        for ext in ['mp3', 'flac', 'wav', 'ogg', 'm4a', 'aac', 'wma']:
            filt.add_pattern(f'*.{ext}')
        dlg.add_filter(filt)
        if dlg.run() == Gtk.ResponseType.OK:
            paths = dlg.get_filenames()
            for path in paths:
                if path not in [item[0] for item in self.playlist]:
                    song_name = os.path.splitext(os.path.basename(path))[0]
                    duration_sec = self.get_song_duration_fast(path)
                    self.playlist.append((path, song_name, duration_sec))
                    self.playlist_store.append([path, song_name, duration_sec])
            if len(self.playlist) == 1 and self.current_song_idx == -1:
                self.current_song_idx = 0
                self.load_song(0, auto_play=False)
                self.update_current_song_display()
        dlg.destroy()
        song_count = len(self.playlist)
        self.loading_label.set_markup(f'<span size="small" color="#666666">已加载 {song_count} 首</span>')

    def on_remove_song(self, widget):
        """删除当前播放的歌曲（红色高亮的歌曲）"""
        if self.current_song_idx == -1 or not self.playlist:
            # 没有选中/播放的歌曲，提示用户
            dialog = Gtk.MessageDialog(
                parent=self,
                flags=0,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="无歌曲可删除",
            )
            dialog.format_secondary_text("当前没有正在播放的歌曲，请选择要删除的歌曲后重试")
            dialog.run()
            dialog.destroy()
            return

        # 停止当前播放的歌曲
        if self.play_flag:
            self.player.set_state(Gst.State.READY)
            self.play_flag = False
            self.btn_play.set_tooltip_text("播放")
            icon = Gtk.Image.new_from_icon_name("media-playback-start", Gtk.IconSize.BUTTON)
            self.btn_play.set_image(icon)

        # 从播放列表中删除对应条目
        del self.playlist[self.current_song_idx]
        # 从TreeView的Store中删除对应行
        tree_iter = self.playlist_store.get_iter(self.current_song_idx)
        if tree_iter:
            self.playlist_store.remove(tree_iter)

        # 处理随机播放模式的列表
        if self.play_mode == 3 and self.random_playlist:
            # 找到随机列表中对应的歌曲并删除
            for i, song in enumerate(self.random_playlist):
                if song[0] == self.playlist[self.current_song_idx][0] if self.playlist else None:
                    del self.random_playlist[i]
                    self.random_index = (self.random_index - 1) % len(self.random_playlist) if self.random_playlist else -1
                    break

        # 更新状态
        song_count = len(self.playlist)
        self.loading_label.set_markup(f'<span size="small" color="#666666">已加载 {song_count} 首</span>')
    
        # 重置当前播放索引
        if song_count == 0:
            # 列表为空
            self.current_song_idx = -1
            self.current_duration = 0.0
            self.reset_lrc_display()
            self.label_duration.set_label('--:-- / --:--')
            self.scale.set_value(0)
        else:
            # 列表还有歌曲，切换到下一首（或最后一首）
            self.current_song_idx = min(self.current_song_idx, song_count - 1)
            self.load_song(self.current_song_idx, auto_play=False)

        # 更新当前播放歌曲显示
        self.update_current_song_display()

    def on_clear_playlist(self, widget):
        """清空播放列表"""
        self.playlist.clear()
        self.playlist_store.clear()
        self.current_song_idx = -1
        self.play_flag = False
        self.player.set_state(Gst.State.READY)
        self.btn_play.set_tooltip_text("播放")
        icon = Gtk.Image.new_from_icon_name("media-playback-start", Gtk.IconSize.BUTTON)
        self.btn_play.set_image(icon)
        self.reset_lrc_display()
        self.label_duration.set_label('--:-- / --:--')  # 恢复占位符
        self.update_current_song_display()

    def on_prev_song(self, widget):
        """上一曲"""
        if not self.playlist:
            return
            
        # 随机播放模式处理
        if self.play_mode == 3 and self.random_playlist:
            self.random_index = (self.random_index - 1) % len(self.random_playlist)
            # 找到随机列表中歌曲在原列表的索引
            random_song = self.random_playlist[self.random_index]
            for idx, song in enumerate(self.playlist):
                if song[0] == random_song[0]:
                    self.current_song_idx = idx
                    break
        else:
            if self.current_song_idx <= 0:
                if self.play_mode == 1:
                    self.current_song_idx = len(self.playlist) - 1
                else:
                    self.current_song_idx = 0
            else:
                self.current_song_idx -= 1
                
        self.load_song(self.current_song_idx)
        self.update_current_song_display()

    def on_next_song(self, widget):
        """下一曲"""
        if not self.playlist:
            return
            
        # 随机播放模式处理
        if self.play_mode == 3 and self.random_playlist:
            self.random_index = (self.random_index + 1) % len(self.random_playlist)
            # 找到随机列表中歌曲在原列表的索引
            random_song = self.random_playlist[self.random_index]
            for idx, song in enumerate(self.playlist):
                if song[0] == random_song[0]:
                    self.current_song_idx = idx
                    break
        else:
            if self.current_song_idx >= len(self.playlist) - 1:
                if self.play_mode == 1:
                    self.current_song_idx = 0
                else:
                    self.current_song_idx = len(self.playlist) - 1
            else:
                self.current_song_idx += 1
                
        self.load_song(self.current_song_idx)
        self.update_current_song_display()

    def on_play(self, widget):
        """播放/暂停"""
        if not self.playlist or self.current_song_idx == -1:
            if self.playlist:
                self.current_song_idx = 0
                self.load_song(0)
        else:
            if not self.play_flag:
                self.player.set_state(Gst.State.PLAYING)
                self.play_flag = True
                self.btn_play.set_tooltip_text("暂停")
                icon = Gtk.Image.new_from_icon_name("media-playback-pause", Gtk.IconSize.BUTTON)
                self.btn_play.set_image(icon)
            else:
                self.player.set_state(Gst.State.PAUSED)
                self.play_flag = False
                self.btn_play.set_tooltip_text("播放")
                icon = Gtk.Image.new_from_icon_name("media-playback-start", Gtk.IconSize.BUTTON)
                self.btn_play.set_image(icon)
        self.update_current_song_display()

    def on_stop(self, widget):
        """停止播放（优化时长显示）"""
        self.player.set_state(Gst.State.READY)
        self.play_flag = False
        self.btn_play.set_tooltip_text("播放")
        icon = Gtk.Image.new_from_icon_name("media-playback-start", Gtk.IconSize.BUTTON)
        self.btn_play.set_image(icon)
        self.curr_pos = 0.0
        self.scale.set_value(0)
        self.current_lrc_index = -1
        self.highlight_current_lrc(-1)
        
        # 保留总时长，仅重置当前进度
        if self.current_duration > 0:
            self.label_duration.set_label(f"00:00 / {self.format_time(self.current_duration)}")
        else:
            self.label_duration.set_label('--:-- / --:--')
            
        self.update_current_song_display()

    def on_eos(self, bus, msg):
        """播放结束处理"""
        if self.play_mode == 2:
            self.player.seek_simple(
                Gst.Format.TIME, 
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 
                0
            )
            self.player.set_state(Gst.State.PLAYING)
        else:
            self.on_next_song(None)
        self.update_current_song_display()

    def on_error(self, bus, msg):
        """播放错误处理"""
        err, debug = msg.parse_error()
        print(f"播放错误: {err.message} (调试信息: {debug})")

    def get_pos(self):
        """获取当前播放位置和总时长（增强异常处理）"""
        try:
            # 安全获取时长
            success_dur, dur_ns = self.player.query_duration(Gst.Format.TIME)
            if not success_dur:
                dur = self.current_duration
            else:
                dur = dur_ns / Gst.SECOND
                dur = max(0, dur)  # 确保非负
            
            # 安全获取进度
            success_pos, pos_ns = self.player.query_position(Gst.Format.TIME)
            if not success_pos:
                pos = 0.0
            else:
                pos = pos_ns / Gst.SECOND
                pos = max(0, min(pos, dur))  # 限制进度在0~总时长之间
            
            # 更新缓存时长
            if FAST_LOAD and self.current_duration == 0.0 and dur > 0:
                self.current_duration = dur
                idx = self.current_song_idx
                if idx >=0 and idx < len(self.playlist):
                    path, name, _ = self.playlist[idx]
                    self.playlist[idx] = (path, name, dur)
            
            return pos, dur
        except:
            return 0.0, max(0, self.current_duration)

    def on_seek(self, widget, event):
        """进度条拖动跳转（增加边界校验）"""
        if not self.playlist or self.current_song_idx == -1:
            return
        pos, dur = self.get_pos()
        if dur <= 0:
            return
            
        val = self.scale.get_value()
        seek_pos = dur * val / 100
        # 限制跳转范围在合法区间
        seek_pos = max(0, min(seek_pos, dur))
        
        self.player.seek_simple(
            Gst.Format.TIME, 
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 
            seek_pos * Gst.SECOND
        )
        self.current_lrc_index = self.lrc.get_current_line_index(seek_pos)
        self.highlight_current_lrc(self.current_lrc_index)
        self.label_duration.set_label(f"{self.format_time(seek_pos)} / {self.format_time(dur)}")

    def update_ui(self):
        """定时更新播放进度（优化显示逻辑）"""
        if self.play_flag and self.playlist and self.current_song_idx != -1:
            pos, dur = self.get_pos()
            self.curr_pos = pos
            
            # 只有当时长有效时才更新进度条和显示
            if dur > 0:
                self.scale.set_value(pos / dur * 100)
                self.label_duration.set_label(f"{self.format_time(pos)} / {self.format_time(dur)}")
                
                # 更新歌词高亮
                new_lrc_idx = self.lrc.get_current_line_index(pos)
                if new_lrc_idx != self.current_lrc_index:
                    self.current_lrc_index = new_lrc_idx
                    self.highlight_current_lrc(new_lrc_idx)
            else:
                # 时长未加载完成时显示占位符
                self.label_duration.set_label('--:-- / --:--')
        
        self.update_current_song_display()
        return True

if __name__ == '__main__':
    win = MusicPlayer()
    win.connect('destroy', Gtk.main_quit)
    win.show_all()
    Gtk.main()