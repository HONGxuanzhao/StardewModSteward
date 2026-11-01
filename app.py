"""Chinese desktop UI for Stardew Mod Steward. Run with python app.py."""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

from PySide6.QtCore import (Qt, QObject, QRunnable, QThreadPool, Signal, QTimer, QUrl, QLockFile, QItemSelectionModel)
from PySide6.QtGui import QFont, QColor, QIcon, QDesktopServices, QPainter, QPixmap, QPen
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSplitter, QTextBrowser, QPlainTextEdit, QProgressBar, QDialog, QFormLayout, QFileDialog,
    QCheckBox, QMessageBox, QDialogButtonBox, QComboBox, QFrame)

from core import (APP_VERSION, CACHE_SCHEMA, DATA, Settings, ModError, scan, installed_versions, check_updates,
    apply_cache, write_json, Nexus, InstallPlan, mutation_lock, backup_list, restore_backup,
    ModStatePlan, state_companions, validate_game, ensure_stopped, discover_games)

STYLE = """
* { font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; color: #253d35; }
QMainWindow, QDialog { background: #f5f7f3; }
QWidget#sidebar { background: #e7eee5; border-right: 1px solid #d6dfd3; }
QLabel#brand { font-size: 22px; font-weight: 700; color: #235a3c; }
QLabel#title { font-size: 27px; font-weight: 700; }
QLabel#muted { color: #78857b; font-size: 12px; }
QLabel#subtitle { color: #6c7c70; }
QLabel#number { font-family: 'Segoe UI'; font-size: 28px; font-weight: 700; color: #235a3c; }
QFrame#card { background: #ffffff; border: 1px solid #e0e6dc; border-radius: 12px; }
QPushButton { background: #ffffff; border: 1px solid #d5dfd3; padding: 9px 15px; border-radius: 7px; }
QPushButton:hover { background: #edf4e9; border-color: #9fb49c; }
QPushButton:pressed { background: #dde9d8; }
QPushButton:disabled { color: #a3aea2; background: #f1f3ee; border-color: #e3e8df; }
QPushButton#primary { background: #356c47; color: white; border-color: #356c47; font-weight: 600; }
QPushButton#primary:hover { background: #285a3a; }
QPushButton#primary:disabled { background: #a4b8a7; border-color: #a4b8a7; }
QPushButton#nav { text-align: left; border: 0; background: transparent; padding: 12px 15px; }
QPushButton#nav:checked { background: #d2e2cf; color: #235a3c; font-weight: 700; }
QLineEdit, QComboBox { background: white; border: 1px solid #d9e2d5; padding: 9px; border-radius: 7px; }
QLineEdit:focus { border-color: #578568; }
QTableWidget { background: white; border: 1px solid #e0e6dc; border-radius: 9px;
              gridline-color: #edf0e9; selection-background-color: #e8f1e3; selection-color: #235a3c; }
QTableWidget::item { padding-left: 12px; padding-right: 8px; border-bottom: 1px solid #f0f2ed; }
QHeaderView::section { background: #edf2e9; color: #637560; border: 0; padding: 11px; font-weight: 600; }
QTextBrowser, QPlainTextEdit { background: white; border: 1px solid #e0e6dc; border-radius: 9px; padding: 12px; }
QProgressBar { border: none; background: #e3eade; border-radius: 3px; height: 5px; }
QProgressBar::chunk { background: #7d9e66; border-radius: 3px; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 17px; height: 17px; }
QSplitter::handle { background: transparent; width: 10px; }
QScrollBar:vertical { background: #f4f6f0; width: 10px; border-radius: 5px; }
QScrollBar::handle:vertical { background: #c8d5c2; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QToolTip { background: #fff; color: #253d35; padding: 7px; border: 1px solid #cbd9c3; }
"""


def make_icon():
    pix = QPixmap(128, 128)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#356c47"))
    p.drawRoundedRect(2, 2, 124, 124, 28, 28)
    p.setBrush(QColor("#f4db8d"))
    p.drawEllipse(35, 48, 58, 54)
    p.setBrush(QColor("#91b66e"))
    p.drawEllipse(63, 24, 33, 21)
    p.setPen(QPen(QColor("#d4e8ab"), 6))
    p.drawLine(63, 55, 70, 34)
    p.end()
    return QIcon(pix)


def button(text, fn=None, primary=False):
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        b.setObjectName("primary")
    if fn:
        b.clicked.connect(fn)
    return b


def label(text, name=None):
    item = QLabel(text)
    if name:
        item.setObjectName(name)
    return item


class JobSignals(QObject):
    log = Signal(str)
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Job(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = JobSignals()

    def run(self):
        try:
            self.signals.result.emit(self.fn(self.signals.log.emit))
        except Exception as e:
            # Exception traces never include local variables or HTTP headers.
            DATA.mkdir(parents=True, exist_ok=True)
            with (DATA / "errors.log").open("a", encoding="utf-8") as f:
                f.write(time.strftime("\n%Y-%m-%d %H:%M:%S\n") + traceback.format_exc())
            self.signals.error.emit(str(e) if isinstance(e, ModError) else f"{type(e).__name__}: {e}")
        finally:
            self.signals.finished.emit()


class SettingsDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("设置 · 星露谷 MOD 管家")
        self.resize(820, 530)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.addWidget(label("连接游戏与 Nexus", "brand"))
        text = label("无需 Nexus 密钥也能检查更新、导入安装包和恢复备份。", "subtitle")
        text.setWordWrap(True)
        layout.addWidget(text)
        form = QFormLayout()
        self.path = QLineEdit(owner.game)
        row = QHBoxLayout()
        row.addWidget(self.path)
        row.addWidget(button("浏览", self.browse))
        row.addWidget(button("自动探测", self.detect_paths))
        form.addRow("游戏 / Mods 目录", row)
        self.detected_paths = QComboBox()
        self.detected_paths.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.detected_paths.setMinimumContentsLength(30)
        self.detected_paths.currentIndexChanged.connect(self.choose_detected)
        form.addRow("发现的安装", self.detected_paths)
        self.detect_hint = label("", "muted")
        self.detect_hint.setWordWrap(True)
        form.addRow("", self.detect_hint)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("粘贴个人 API Key；保存时由 Windows 加密")
        try:
            self.key.setText(owner.settings.key())
        except ModError:
            pass
        form.addRow("Nexus API Key", self.key)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.validate_button = button("验证账号", self.validate)
        row.addWidget(self.validate_button)
        row.addWidget(button("获取个人 API Key ↗", lambda: owner.open_url("https://www.nexusmods.com/users/myaccount?tab=api")))
        row.addStretch()
        layout.addLayout(row)
        self.account = label("全自动下载需要 Nexus Premium；普通账号请网页下载后拖入安装。", "subtitle")
        self.account.setWordWrap(True)
        layout.addWidget(self.account)
        self.auto_check = QCheckBox("软件打开时、以及每小时自动检查更新")
        self.auto_check.setChecked(owner.settings.values.get("auto_check", True))
        self.auto_update = QCheckBox("自动安装可明确匹配的推荐更新（需要 Premium；游戏运行时跳过）")
        self.auto_update.setChecked(owner.settings.values.get("auto_update", False))
        self.preserve = QCheckBox("更新时保留已有 config.json（旧目录始终完整备份）")
        self.preserve.setChecked(owner.settings.values.get("preserve_config", True))
        for cb in (self.auto_check, self.auto_update, self.preserve):
            layout.addWidget(cb)
        layout.addStretch()
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Save).setText("保存设置")
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        box.accepted.connect(self.save)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self.jobs = set()
        self.detect_paths()

    def browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择星露谷游戏目录", self.path.text())
        if path:
            self.path.setText(path)

    def detect_paths(self):
        current = self.path.text().strip()
        self.installations = discover_games([current])
        self.detected_paths.blockSignals(True)
        self.detected_paths.clear()
        if not self.installations:
            self.detected_paths.addItem("未找到带 SMAPI 的安装，请点击浏览选择")
            self.detect_hint.setText("已检查 Steam 注册表、Steam 游戏库、最近 SMAPI 日志和常见安装目录。")
        else:
            for row in self.installations:
                self.detected_paths.addItem(row["game"] + "  ·  " + " / ".join(row["sources"]))
            self.path.setText(self.installations[0]["game"])
            self.detected_paths.setToolTip(self.installations[0]["game"])
            self.detect_hint.setText(f"发现 {len(self.installations)} 个安装；MOD 目录：{self.installations[0]['mods']}")
        self.detected_paths.blockSignals(False)

    def choose_detected(self, index):
        if 0 <= index < len(getattr(self, "installations", [])):
            row = self.installations[index]
            self.path.setText(row["game"])
            self.detected_paths.setToolTip(row["game"])
            self.detect_hint.setText("MOD 目录：" + row["mods"])

    def validate(self):
        key = self.key.text().strip()
        job = Job(lambda log: Nexus(key).validate())
        self.jobs.add(job)
        self.validate_button.setEnabled(False)
        self.account.setText("正在连接 Nexus …")
        job.signals.result.connect(self.validated)
        job.signals.error.connect(lambda message: self.account.setText(message))
        job.signals.finished.connect(lambda: self.validate_button.setEnabled(True))
        job.signals.finished.connect(lambda: self.jobs.discard(job))
        QThreadPool.globalInstance().start(job)

    def validated(self, user):
        kind = "Premium，可自动下载" if user.get("is_premium") else "普通账号，使用网页下载 + 导入安装"
        self.account.setText(f"已验证：{user.get('name', 'Nexus 用户')} · {kind}")

    def save(self):
        try:
            game = str(validate_game(self.path.text().strip()))
            if self.auto_update.isChecked() and not self.key.text().strip():
                raise ModError("启用自动安装前请填写并验证 Nexus Premium API Key。")
            self.owner.settings.set_key(self.key.text())
            self.owner.settings.values.update(game=game, auto_check=self.auto_check.isChecked(),
                auto_update=self.auto_update.isChecked(), preserve_config=self.preserve.isChecked())
            self.owner.settings.values["game_source"] = next((" / ".join(r["sources"]) for r in self.installations
                if r["game"].casefold() == game.casefold()), "手动选择")
            self.owner.settings.save()
            self.accept()
        except Exception as e:
            QMessageBox.warning(self, "无法保存", str(e))

    def closeEvent(self, event):
        if self.jobs:
            event.ignore()
        else:
            super().closeEvent(event)

    def done(self, result):
        if self.jobs:
            return
        super().done(result)


class BackupDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("备份与恢复")
        self.resize(840, 460)
        layout = QVBoxLayout(self)
        layout.addWidget(label("每次安装都会保存原来的完整 MOD 目录。恢复前也会保留当前文件。", "subtitle"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["备份时间", "状态", "涉及 MOD"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 130)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        layout.addWidget(self.table)
        self.rows = backup_list(owner.game)
        state_names = {"complete": "可恢复", "prepared": "安装中断", "recovery_required": "需要恢复",
                       "restored": "已恢复", "rolled_back": "已自动回滚"}
        for i, row in enumerate(self.rows):
            self.table.insertRow(i)
            values = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["time"])),
                      state_names.get(row["state"], row["state"]),
                      (row.get("action", "安装") + "：" if row.get("kind") == "mod_state" else "") +
                      "、".join(x["name"] for x in row["entries"])]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(i, col, item)
        if not self.rows:
            layout.addWidget(label("暂无备份。首次安装或更新后会出现在这里。", "muted"))
        row = QHBoxLayout()
        row.addWidget(button("打开备份目录", self.open_folder))
        row.addStretch()
        row.addWidget(button("恢复选中的备份", self.restore, True))
        row.addWidget(button("关闭", self.accept))
        layout.addLayout(row)

    def open_folder(self):
        root = Path(self.owner.game) / ".stardew-mod-steward" / "backups"
        root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def restore(self):
        row = self.table.currentRow()
        if row < 0:
            return
        chosen = self.rows[row]
        if chosen["state"] not in ("complete", "prepared", "recovery_required"):
            QMessageBox.information(self, "提示", "此备份已恢复或回滚。")
            return
        prompt = "撤销此批次的启用 / 禁用操作？会重新检查依赖关系。" if chosen.get("kind") == "mod_state" else "恢复此批次的原版本？当前版本也会保留在备份目录。"
        if QMessageBox.question(self, "恢复 MOD", prompt) != QMessageBox.StandardButton.Yes:
            return
        game = self.owner.game
        def run(log):
            return restore_backup(game, chosen["folder"], log)
        self.accept()
        self.owner.run_job(run, self.owner.after_install, "正在恢复备份 …")


class Window(QMainWindow):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.game = settings.values["game"]
        self.mods = []
        self.cache = {}
        self.filter_name = "all"
        self.busy = False
        self.jobs = set()
        self.actions = []
        self.setWindowTitle("星露谷 MOD 管家")
        self.setWindowIcon(make_icon())
        self.resize(1320, 860)
        self.setMinimumSize(1080, 720)
        self.setAcceptDrops(True)
        self.build()
        self.refresh()
        self.timer = QTimer(self)
        self.timer.setInterval(60 * 60 * 1000)
        self.timer.timeout.connect(self.periodic)
        self.timer.start()
        QTimer.singleShot(1600, self.periodic)

    def build(self):
        central = QWidget()
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(central)
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(205)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(18, 30, 18, 23)
        sl.setSpacing(9)
        sl.addWidget(label("星露谷", "brand"))
        sl.addWidget(label("MOD 管家", "brand"))
        sl.addWidget(label("STARDEW MOD STEWARD", "muted"))
        sl.addSpacing(34)
        self.nav = {}
        for key, title in [("all", "全部 MOD"), ("enabled", "已启用"), ("disabled", "已禁用"),
                           ("updates", "可用更新"), ("issues", "需要检查")]:
            b = button(title, lambda checked=False, k=key: self.set_filter(k))
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setChecked(key == "all")
            sl.addWidget(b)
            self.nav[key] = b
        sl.addStretch()
        b = button("备份与恢复", self.backups)
        sl.addWidget(b)
        self.actions.append(b)
        b = button("设置 / Nexus 账号", self.configure)
        sl.addWidget(b)
        self.actions.append(b)
        sl.addSpacing(14)
        self.account_label = label("Nexus 尚未连接", "muted")
        self.account_label.setWordWrap(True)
        sl.addWidget(self.account_label)
        sl.addWidget(label("v" + APP_VERSION + "  ·  本地桌面工具", "muted"))
        outer.addWidget(side)
        body = QVBoxLayout()
        body.setContentsMargins(30, 25, 30, 20)
        body.setSpacing(16)
        outer.addLayout(body, 1)
        top = QHBoxLayout()
        titlecol = QVBoxLayout()
        titlecol.addWidget(label("让每一份 MOD 井然有序", "title"))
        titlecol.addWidget(label("查看版本、检查更新，安心管理你的农场。", "subtitle"))
        top.addLayout(titlecol)
        top.addStretch()
        b = button("启动 SMAPI  ↗", self.launch, True)
        top.addWidget(b)
        self.actions.append(b)
        body.addLayout(top)
        self.path_label = label(self.game, "muted")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.path_label)
        cards = QHBoxLayout()
        self.counts = []
        for title in ("已识别 MOD", "可用更新", "需要检查", "已禁用"):
            card = QFrame()
            card.setObjectName("card")
            lay = QHBoxLayout(card)
            lay.setContentsMargins(18, 12, 18, 12)
            lay.addWidget(label(title, "subtitle"))
            lay.addStretch()
            n = label("—", "number")
            lay.addWidget(n)
            self.counts.append(n)
            cards.addWidget(card)
        body.addLayout(cards)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称、作者或 UniqueID …")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.populate)
        bar.addWidget(self.search, 1)
        self.check_button = button("检查更新", self.check, True)
        bar.addWidget(self.check_button)
        self.actions.append(self.check_button)
        for title, fn in [("一键更新全部", self.update_all), ("导入压缩包", self.import_dialog), ("刷新", self.refresh)]:
            b = button(title, fn)
            bar.addWidget(b)
            self.actions.append(b)
        body.addLayout(bar)
        states = QHBoxLayout()
        states.addWidget(label("启用状态", "subtitle"))
        self.enable_selected = button("启用选中", lambda: self.change_state(True))
        self.disable_selected = button("禁用选中", lambda: self.change_state(False))
        states.addWidget(self.enable_selected)
        states.addWidget(self.disable_selected)
        self.actions.extend([self.enable_selected, self.disable_selected])
        self.selection_label = label("勾选左侧状态可单独切换；Ctrl / Shift 可多选", "muted")
        states.addWidget(self.selection_label, 1)
        body.addLayout(states)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["启用", "MOD 名称", "已安装", "推荐版本", "状态", "来源"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col, width in [(0, 58), (2, 90), (3, 90), (4, 110), (5, 85)]:
            self.table.setColumnWidth(col, width)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.itemSelectionChanged.connect(self.show_details)
        self.table.itemChanged.connect(self.state_checkbox_changed)
        self.table.doubleClicked.connect(lambda index: self.open_mod() if index.column() != 0 else None)
        split.addWidget(self.table)
        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(4, 0, 0, 0)
        dl.setSpacing(10)
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        self.detail.anchorClicked.connect(lambda url: self.open_url(url.toString()))
        dl.addWidget(self.detail, 1)
        row = QHBoxLayout()
        self.modpage = button("MOD 网页 ↗", self.open_mod)
        self.folder = button("本地目录", self.open_folder)
        row.addWidget(self.modpage)
        row.addWidget(self.folder)
        dl.addLayout(row)
        row = QHBoxLayout()
        self.update_one = button("更新此 MOD", self.update_selected, True)
        self.toggle_button = button("禁用", self.toggle)
        row.addWidget(self.update_one)
        row.addWidget(self.toggle_button)
        dl.addLayout(row)
        self.actions.extend([self.modpage, self.folder, self.update_one, self.toggle_button])
        split.addWidget(detail)
        split.setSizes([730, 285])
        split.setCollapsible(0, False)
        split.setCollapsible(1, False)
        body.addWidget(split, 1)
        self.hint = label("普通账号：网页下载后，将 ZIP / 7Z / RAR 拖入此窗口即可安装。", "subtitle")
        self.hint.setWordWrap(True)
        body.addWidget(self.hint)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(500)
        self.logs.setFixedHeight(100)
        self.logs.setPlaceholderText("操作记录会显示在这里")
        body.addWidget(self.logs)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFixedHeight(5)
        body.addWidget(self.progress)
        self.status_label = label("准备就绪", "muted")
        body.addWidget(self.status_label)

    def log(self, message):
        self.logs.appendPlainText(time.strftime("%H:%M:%S  ") + message)
        self.status_label.setText(message)
        self.status_label.setToolTip(message)
        DATA.mkdir(parents=True, exist_ok=True)
        with (DATA / "activity.log").open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + message + "\n")

    def refresh(self):
        if self.busy:
            return
        try:
            self.game = self.settings.values["game"]
            self.mods, errors = scan(self.game)
            cache_path = DATA / "updates.json"
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
                if not isinstance(self.cache, dict):
                    self.cache = {}
            except (OSError, ValueError):
                self.cache = {}
                self.log("更新缓存无法读取，可重新点击检查更新。")
            apply_cache(self.mods, self.cache, self.game)
            api, game = installed_versions(self.game)
            self.path_label.setText(f"MOD 目录：{Path(self.game) / 'Mods'}    ·    SMAPI {api}    /    游戏 {game or '未知'}")
            self.path_label.setToolTip("游戏目录：" + self.game + "\n发现依据：" + self.settings.values.get("game_source", "手动选择"))
            if self.settings.values.get("nexus_key"):
                self.account_label.setText("Nexus API Key 已保存\n可在设置中验证账号")
            else:
                self.account_label.setText("Nexus 尚未连接\n检查更新无需密钥")
            self.scan_errors = errors
            self.populate()
            self.log(f"已扫描 {len(self.mods)} 份 MOD 清单。" + (f"另有 {len(errors)} 个读取问题。" if errors else ""))
            for error in errors:
                self.log(error)
            incomplete = [b for b in backup_list(self.game) if b.get("state") in ("prepared", "recovery_required")]
            if incomplete:
                self.log("发现未完成的安装记录，请打开「备份与恢复」检查。")
            if self.cache.get("schema") == CACHE_SCHEMA and self.cache.get("time") and self.cache.get("game", "").casefold() == self.game.casefold():
                stamp = time.strftime("%m-%d %H:%M", time.localtime(self.cache["time"]))
                self.status_label.setText(f"上次更新检查：{stamp}（缓存结果）  ·  {len(self.mods)} 份 MOD 清单")
        except Exception as e:
            self.log("扫描失败：" + str(e))
            self.hint.setText("请在「设置」中选择含 SMAPI 的星露谷游戏目录。")

    def set_filter(self, key):
        self.filter_name = key
        for k, b in self.nav.items():
            b.setChecked(k == key)
        self.populate()

    def populate(self):
        selected = self.selected()
        selected_path = selected.path if selected else None
        selected_paths = getattr(self, "_state_selection", None)
        if selected_paths is None:
            selected_paths = {str(m.path) for m in self.selected_mods()}
        self._state_selection = None
        totals = [len(self.mods), sum(bool(m.suggestion) for m in self.mods),
                  sum(m.requires_attention for m in self.mods),
                  sum(m.disabled for m in self.mods)]
        for l, n in zip(self.counts, totals):
            l.setText(str(n))
        navigation = {"all": ("全部 MOD", totals[0]), "updates": ("可用更新", totals[1]),
                      "issues": ("需要检查", totals[2]), "disabled": ("已禁用", totals[3]),
                      "enabled": ("已启用", totals[0] - totals[3])}
        for key, (text, count) in navigation.items():
            self.nav[key].setText(f"{text}    {count}")
        needle = self.search.text().strip().casefold()
        visible = [m for m in self.mods if needle in f"{m.name} {m.author} {m.uid}".casefold()]
        if self.filter_name == "updates":
            visible = [m for m in visible if m.suggestion]
        elif self.filter_name == "issues":
            visible = [m for m in visible if m.requires_attention]
        elif self.filter_name == "disabled":
            visible = [m for m in visible if m.disabled]
        elif self.filter_name == "enabled":
            visible = [m for m in visible if not m.disabled]
        self.table.blockSignals(True)
        self.table.setRowCount(len(visible))
        self.visible = visible
        wanted = 0
        selected_rows = []
        for i, m in enumerate(visible):
            state = QTableWidgetItem()
            state.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            state.setCheckState(Qt.CheckState.Unchecked if m.disabled else Qt.CheckState.Checked)
            state.setToolTip("点击启用此 MOD" if m.disabled else "点击禁用此 MOD")
            self.table.setItem(i, 0, state)
            values = [m.name, m.version, m.suggestion or ("见主包" if m.update.get("bundle") else "—"), m.status,
                      m.source_label]
            for col, text in enumerate(values, 1):
                item = QTableWidgetItem(text)
                item.setToolTip(text if col != 1 else f"{m.name}\n{m.uid}\n{m.path}")
                if col == 4:
                    color = "#b27729" if m.requires_attention else "#2c7c43" if m.suggestion else "#798574"
                    item.setForeground(QColor(color))
                self.table.setItem(i, col, item)
            self.table.setRowHeight(i, 43)
            if m.path == selected_path:
                wanted = i
            if str(m.path) in selected_paths:
                selected_rows.append(i)
        self.table.clearSelection()
        if visible:
            rows = selected_rows or [wanted]
            for row in rows:
                self.table.selectionModel().select(self.table.model().index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
            self.table.setCurrentCell(wanted if wanted in rows else rows[0], 1,
                                      QItemSelectionModel.SelectionFlag.NoUpdate)
        self.table.blockSignals(False)
        self.show_details()

    def selected_mods(self):
        if not hasattr(self, "visible"):
            return []
        return [self.visible[index.row()] for index in sorted(self.table.selectionModel().selectedRows(), key=lambda i: i.row())
                if 0 <= index.row() < len(self.visible)]

    def selected(self):
        row = self.table.currentRow()
        return self.visible[row] if hasattr(self, "visible") and 0 <= row < len(self.visible) else None

    def show_details(self):
        m = self.selected()
        chosen = self.selected_mods()
        self.enable_selected.setEnabled(not self.busy and any(mod.disabled for mod in chosen))
        self.disable_selected.setEnabled(not self.busy and any(not mod.disabled for mod in chosen))
        self.toggle_button.setEnabled(not self.busy and m is not None)
        self.selection_label.setText(f"已选 {len(chosen)} 项 · 勾选状态可单独切换，Ctrl / Shift 可多选")
        if not m:
            self.detail.setHtml("<h3>选择一个 MOD</h3><p>查看版本、来源和依赖关系。</p>")
            return
        esc = lambda value: html.escape(str(value))
        def tag(text):
            return f'<p style="color:#99621d">{esc(text)}</p>'
        body = f'<h2 style="color:#235a3c">{esc(m.name)}</h2><p style="color:#7a8976">{esc(m.author)}</p>'
        body += '<p><b>启用状态</b>　' + ("已禁用" if m.disabled else "已启用") + '</p>'
        companions = state_companions(self.game, [m], self.mods)
        if companions:
            body += '<p><b>配套组件</b><br>' + '<br>'.join(
                esc(other.name) + ("（已禁用）" if other.disabled else "（已启用）") for other in companions) + '</p>'
            body += '<p>一个 MOD 可能由多个组件组成；单独禁用此项时，其他组件仍可能被 SMAPI 加载。启停确认窗口可同时调整配套组件。</p>'
        body += f'<p><b>已安装</b>　{esc(m.version)}<br><b>推荐版本</b>　{esc(m.suggestion or "暂无推荐")}</p>'
        body += f'<p>{esc(m.description)}</p><hr><p><b>UniqueID</b><br>{esc(m.uid)}</p>'
        body += f'<p><b>本地目录</b><br>{esc(str(m.path))}</p>'
        body += '<p><b>更新来源</b><br>' + '<br>'.join(map(esc, m.keys or ["清单未提供更新来源"])) + '</p>'
        effective = m.update.get("effectiveUpdateKeys")
        if effective:
            body += '<p><b>实际查询来源</b><br>' + '<br>'.join(map(esc, effective)) + '</p>'
        if m.update.get("bundle"):
            owner = m.update["bundle"]
            body += f'<p><b>所属主包</b><br>{esc(owner["name"])}<br>主包已装：{esc(owner["installedVersion"])}'
            body += f'<br>主包远端：{esc(owner["remoteVersion"])}</p>'
            body += '<p>组件版本独立显示；请通过主包统一更新，不能据主包版本判定此组件已是最新。</p>'
        for note in m.update.get("sourceNotes", []):
            body += f'<p style="color:#647c63">{esc(note)}</p>'
        deps = list(m.manifest.get("Dependencies", []))
        if m.manifest.get("ContentPackFor"):
            deps.append(m.manifest["ContentPackFor"])
        if deps:
            body += '<p><b>依赖</b><br>' + '<br>'.join(esc(d.get("UniqueID", "")) +
                ("（可选）" if not d.get("IsRequired", True) else "") for d in deps) + '</p>'
        for issue in m.issues:
            body += tag(issue)
        for err in m.update.get("errors", []):
            body += tag("更新查询：" + str(err))
        for err in m.update.get("sourceWarnings", []):
            body += tag("部分来源不可用：" + str(err))
        meta = m.update.get("metadata") or {}
        if m.has_unofficial_fix:
            body += '<p style="color:#647c63">已装版本号与 SMAPI 目录中的非官方修复版一致；仍需实际游戏验证运行情况。</p>'
        elif meta.get("compatibilitySummary"):
            body += tag(replace_html(meta["compatibilitySummary"]))
        if m.compatibility == "Abandoned":
            body += tag("SMAPI 标记此 MOD 已停止维护。请核对其替代方案，不会自动删除已有 MOD。")
        elif m.compatibility == "Unofficial" and not m.has_unofficial_fix:
            body += tag("官方版本存在兼容性问题，请核对是否已使用适配当前游戏的非官方修复版。")
        body += '<p style="color:#8b9585;font-size:11px">更新结果来自 SMAPI 公共接口。无推荐更新不代表已经验证游戏内兼容性。</p>'
        self.detail.setHtml(body)
        self.toggle_button.setText("启用" if m.disabled else "禁用")
        self.update_one.setText(("核对网页" if m.update["bundle"].get("mode") == "web" else "更新主包")
                               if m.update.get("bundle") else "更新此 MOD")

    def run_job(self, fn, result=None, message="处理中 …"):
        if self.busy:
            return
        self.busy = True
        self.table.setEnabled(False)
        for b in self.actions:
            b.setEnabled(False)
        self.progress.setRange(0, 0)
        self.log(message)
        job = Job(fn)
        self.jobs.add(job)
        job.signals.log.connect(self.log)
        if result:
            job.signals.result.connect(result)
        job.signals.error.connect(self.failed)
        job.signals.finished.connect(lambda: self.finish_job(job))
        QThreadPool.globalInstance().start(job)

    def finish_job(self, job):
        self.jobs.discard(job)
        self.busy = False
        self.table.setEnabled(True)
        for b in self.actions:
            b.setEnabled(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.show_details()

    def failed(self, message):
        self.log("未完成：" + message)
        QMessageBox.warning(self, "操作未完成", message)

    def check(self, automatic=False):
        if self.busy or not self.mods:
            return
        game, mods = self.game, list(self.mods)
        self.run_job(lambda log: check_updates(game, mods, log),
                     lambda cache: self.checked(cache, automatic), "正在检查更新 …")

    def checked(self, cache, automatic):
        self.cache = cache
        write_json(DATA / "updates.json", cache)
        for m in self.mods:
            m.update = {}
        apply_cache(self.mods, cache, self.game)
        self.populate()
        count = sum(bool(m.suggestion) for m in self.mods)
        failures = sum(bool(m.update.get("errors")) for m in self.mods)
        bundled = sum(bool(m.update.get("bundle")) for m in self.mods)
        self.log(f"检查完成：{count} 个推荐更新，{failures} 个查询失败，{bundled} 个组件关联主包。")
        if automatic and self.settings.values.get("auto_update") and count:
            QTimer.singleShot(200, lambda: self.update_all(automatic=True))

    def periodic(self):
        if self.settings.values.get("auto_check") and not self.busy:
            # A fresh scan notices mods changed outside this manager.
            self.refresh()
            self.check(automatic=True)

    def configure(self):
        if self.busy:
            return
        if SettingsDialog(self).exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def backups(self):
        if not self.busy:
            try:
                BackupDialog(self).exec()
            except Exception as e:
                self.failed(str(e))

    def open_url(self, url):
        if QUrl(url).scheme() in ("https", "http"):
            QDesktopServices.openUrl(QUrl(url))

    def open_mod(self):
        m = self.selected()
        if m and m.url:
            self.open_url(m.url)

    def open_folder(self):
        m = self.selected()
        if m:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(m.path)))

    def launch(self):
        try:
            validate_game(self.game)
            ensure_stopped()
            subprocess.Popen([str(Path(self.game) / "StardewModdingAPI.exe")], cwd=self.game)
            self.log("已启动 SMAPI。")
        except Exception as e:
            self.failed(str(e))

    def import_dialog(self):
        if self.busy:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "选择 MOD 更新包", str(Path.home() / "Downloads"),
                                                "MOD 压缩包 (*.zip *.7z *.rar)")
        if filename:
            self.prepare_import(filename)

    def prepare_import(self, filename):
        game = self.game
        self.run_job(lambda log: InstallPlan(game, filename, log=log), self.confirm_plan, "正在准备安装预览 …")

    def confirm_plan(self, plan):
        # The previous worker's finished signal resets busy before the next queued operation.
        QTimer.singleShot(100, lambda: self.confirm_ready(plan))

    def confirm_ready(self, plan):
        box = QMessageBox(self)
        box.setWindowTitle("确认安装")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"将安装 / 更新 {len(plan.items)} 个 MOD。原目录会完整备份。")
        box.setInformativeText(plan.summary()[:2400])
        box.setDetailedText(plan.summary() + "\n\n" + "\n".join(str(i.target) for i in plan.items))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Yes).setText("安装并备份")
        box.button(QMessageBox.StandardButton.Cancel).setText("取消")
        if box.exec() != QMessageBox.StandardButton.Yes:
            plan.close()
            return
        preserve = self.settings.values.get("preserve_config", True)
        def run(log):
            plan.log = log
            try:
                with mutation_lock(plan.game):
                    return plan.execute(preserve)
            finally:
                plan.close()
        self.run_job(run, self.after_install, "正在安装并备份 …")

    def after_install(self, result):
        self.log("操作完成。备份目录：" + str(result))
        QTimer.singleShot(100, self.refresh)

    def update_selected(self):
        m = self.selected()
        if m and m.update.get("bundle"):
            owner = m.update["bundle"]
            if owner.get("mode") == "web":
                self.open_url(owner["url"])
                return
            primary = next((x for x in self.mods if str(x.path) == owner["path"] and x.uid == owner["uid"]), None)
            if primary:
                m = primary
        if not m or not m.suggestion:
            QMessageBox.information(self, "提示", "请先检查更新，并选择有推荐版本的 MOD。")
            return
        self.update_mods([m], automatic=False)

    def update_all(self, automatic=False):
        mods = [m for m in self.mods if m.suggestion and not m.disabled]
        if not mods:
            if not automatic:
                QMessageBox.information(self, "提示", "当前没有推荐更新。请先点击「检查更新」。")
            return
        if not automatic and QMessageBox.question(self, "一键更新", f"检查并更新 {len(mods)} 个 MOD？\n"
                "仅自动选择版本和来源明确匹配的主文件；无法确定的项目会跳过。\n"
                "需要 Nexus Premium；每个安装包都会备份原目录。") != QMessageBox.StandardButton.Yes:
            return
        self.update_mods(mods, automatic)

    def update_mods(self, mods, automatic):
        if self.busy:
            return
        try:
            key = self.settings.key()
            if not key:
                if automatic:
                    self.log("自动更新已跳过：未连接 Nexus Premium。")
                    return
                self.configure()
                return
        except ModError as e:
            self.failed(str(e))
            return
        game = self.game
        preserve = self.settings.values.get("preserve_config", True)
        def run(log):
            ensure_stopped()
            nexus = Nexus(key)
            user = nexus.validate()
            if not user.get("is_premium"):
                raise ModError("此 Nexus 账号没有 Premium 下载权限。请打开 MOD 网页，下载后拖入本窗口安装。")
            visited, success, skipped = set(), 0, 0
            for mod in mods:
                if mod.uid.casefold() in visited:
                    continue
                if mod.requires_attention or not mod.nexus_id:
                    log(f"跳过 {mod.name}：有本机 / 兼容性提示需要核对，或推荐来源不是 Nexus。")
                    skipped += 1
                    continue
                suggested_url = (mod.update.get("suggestedUpdate") or {}).get("url", "")
                if "nexusmods.com/stardewvalley/mods/" not in suggested_url.lower():
                    log(f"跳过 {mod.name}：推荐更新来自其他网站，请在网页下载后导入。")
                    skipped += 1
                    continue
                files = nexus.candidates(mod)
                if len(files) != 1:
                    log(f"跳过 {mod.name}：找到 {len(files)} 个精确匹配的主文件，请在网页确认后导入。")
                    skipped += 1
                    continue
                archive = nexus.download(mod.nexus_id, files[0], log)
                plan = None
                try:
                    plan = InstallPlan(game, archive, {mod.uid: mod.suggestion}, log)
                    # An automated archive may update bundled installed mods, but can't silently add new ones.
                    if any(item.old is None for item in plan.items):
                        raise ModError("压缩包包含额外的新 MOD，请通过导入安装预览确认。")
                    with mutation_lock(game):
                        plan.execute(preserve)
                    visited.update(i.mod.uid.casefold() for i in plan.items)
                    success += 1
                except ModError as e:
                    log(f"跳过 {mod.name}：{e}")
                    skipped += 1
                finally:
                    if plan:
                        plan.close()
            log(f"更新任务完成：成功安装 {success} 个包，跳过 {skipped} 项。")
            return game
        self.run_job(run, lambda _: QTimer.singleShot(100, self.refresh), "正在连接 Nexus 并准备更新 …")

    def state_checkbox_changed(self, item):
        if item.column() != 0 or not 0 <= item.row() < len(self.visible):
            return
        mod = self.visible[item.row()]
        enabled = item.checkState() == Qt.CheckState.Checked
        self.table.blockSignals(True)
        item.setCheckState(Qt.CheckState.Unchecked if mod.disabled else Qt.CheckState.Checked)
        self.table.blockSignals(False)
        if not self.busy and enabled == mod.disabled:
            self.change_state(enabled, [mod])

    def change_state(self, enabled, mods=None):
        if self.busy:
            return
        chosen = list(mods) if mods is not None else self.selected_mods()
        if not chosen:
            return
        try:
            ensure_stopped()
            companions = [mod for mod in state_companions(self.game, chosen, self.mods)
                          if mod.disabled == enabled]
        except Exception as exc:
            self.failed(str(exc))
            return
        if not any(mod.disabled == enabled for mod in chosen) and not companions:
            QMessageBox.information(self, "无需更改", "所选 MOD 已经是该启用状态。")
            return
        action = "启用" if enabled else "禁用"
        box = QMessageBox(self)
        box.setWindowTitle(action + " MOD")
        box.setIcon(QMessageBox.Icon.Question)
        include = None
        if companions:
            include = QCheckBox(f"同时{action}配套组件（推荐）", box)
            include.setChecked(True)
            box.setCheckBox(include)
        def update_preview():
            actual = chosen + (companions if include and include.isChecked() else [])
            actual = [mod for mod in actual if mod.disabled == enabled]
            summary = "\n".join(f"{action}  {mod.name}  ({mod.uid})" for mod in actual)
            box.setText(f"{action} {len(actual)} 个 MOD 组件？")
            note = ""
            if companions:
                note = "发现配套组件：" + "、".join(mod.name for mod in companions) + "。\n"
                if not include.isChecked():
                    note += "仅调整所选项；其他组件保持原状态，仍可能被 SMAPI 加载。\n"
            box.setInformativeText(note + "禁用会将完整目录移到专用禁用区，启用时恢复到原位置。已有存档可能依赖这些 MOD。\n\n" + summary[:1800])
            box.setDetailedText(summary)
        if include:
            include.toggled.connect(update_preview)
        update_preview()
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Yes).setText("确认" + action)
        box.button(QMessageBox.StandardButton.Cancel).setText("取消")
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        if include and include.isChecked():
            chosen.extend(companions)
        try:
            plan = ModStatePlan(self.game, chosen, enabled)
        except Exception as exc:
            self.failed(str(exc))
            return
        def run(log):
            return plan.execute(log)
        self.run_job(run, self.after_state_change, "正在" + action + " MOD …")

    def after_state_change(self, folder):
        if folder:
            record = json.loads((Path(folder) / "transaction.json").read_text(encoding="utf-8"))
            self._state_selection = {str(Path(self.game) / e["destination"]) for e in record["entries"]}
            self.log(f"{record['action']}完成：{len(record['entries'])} 个 MOD。可在「备份与恢复」撤销。")
        QTimer.singleShot(100, self.refresh)

    def toggle(self):
        mod = self.selected()
        if mod:
            self.change_state(mod.disabled, [mod])

    def dragEnterEvent(self, event):
        if not self.busy and event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if len(urls) == 1 and urls[0].isLocalFile() and Path(urls[0].toLocalFile()).suffix.lower() in (".zip", ".7z", ".rar"):
                event.acceptProposedAction()

    def dropEvent(self, event):
        if not self.busy and event.mimeData().hasUrls():
            self.prepare_import(event.mimeData().urls()[0].toLocalFile())
            event.acceptProposedAction()

    def closeEvent(self, event):
        if self.busy:
            QMessageBox.information(self, "正在执行任务", "请等待当前任务结束，再关闭软件。")
            event.ignore()
        else:
            event.accept()


def replace_html(text):
    import re
    return html.unescape(re.sub("<[^>]+>", "", str(text)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--render", help="Render UI to a PNG for release layout verification, then exit")
    parser.add_argument("--no-network", action="store_true")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("StardewModSteward")
    app.setOrganizationName("LocalTools")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    DATA.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(DATA / "desktop.lock"))
    if not args.render and not lock.tryLock(100):
        QMessageBox.information(None, "星露谷 MOD 管家", "软件已经打开，请在任务栏中切换到它。")
        return 0
    try:
        settings = Settings()
    except (OSError, ValueError):
        bad = DATA / "settings.json"
        if bad.exists():
            bad.rename(DATA / ("settings-corrupt-" + time.strftime("%Y%m%d-%H%M%S") + ".json"))
        settings = Settings()
    if args.no_network or args.render:
        settings.values["auto_check"] = False
        settings.values["auto_update"] = False
    window = Window(settings)
    window.show()
    if args.render:
        def render():
            window.grab().save(args.render)
            app.quit()
        QTimer.singleShot(500, render)
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "startup-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
