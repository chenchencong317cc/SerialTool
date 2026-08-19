#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SerialTool —— 仿 SSCOM V5.13.1 界面与功能的串口调试工具（仅串口）
=================================================================
基于 Python 3 + PyQt5 + pyserial，界面布局与功能对标 SSCOM 5.13.1 串口模式：

  * 菜单栏：通讯端口 / 串口设置 / 显示 / 发送 / 多字符串 / 帮助
  * 工具行：清除窗口、打开文件、发送文件、停止、清发送区、最前、保存参数、扩展/隐藏
  * 端口行：端口号、HEX显示、保存数据、接收数据到文件、HEX发送、定时发送、加回车换行
  * 设置行：打开串口(LED)、刷新、更多串口设置、加时间戳和分包显示、超时时间、
            第N字节至末尾、加校验(None/CRC16/ADD/XOR)
  * 左侧栏：RTS/DTR、波特率、发送按钮
  * 状态栏：S:发送计数 R:接收计数 串口状态
  * 扩展面板：99 条多字符串发送（HEX/注释/顺序/延时/循环发送/导入导出 SSCOM ini）
  * 参数自动保存到 serialtool.ini，启动自动恢复
"""

import datetime
import os
import re
import sys

import serial
from serial.tools import list_ports

from PyQt5.QtCore import Qt, QThread, QTimer, QSettings, pyqtSignal, QEvent
from PyQt5.QtGui import (
    QFont, QTextCursor, QPixmap, QPainter, QColor, QTextCharFormat, QBrush,
    QIcon,
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QGroupBox, QDialog,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout, QLabel, QComboBox,
    QPushButton, QCheckBox, QSpinBox, QLineEdit, QPlainTextEdit, QScrollArea,
    QFileDialog, QMessageBox, QInputDialog, QSizePolicy, QAction, QDialogButtonBox,
)

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------
BAUD_RATES = ["1200", "2400", "4800", "9600", "14400", "19200", "38400",
              "43000", "57600", "76800", "115200", "128000", "230400",
              "256000", "460800", "500000", "576000", "921600", "1000000",
              "1152000", "1500000", "2000000", "3000000"]
DATA_BITS = [("5", serial.FIVEBITS), ("6", serial.SIXBITS),
             ("7", serial.SEVENBITS), ("8", serial.EIGHTBITS)]
PARITIES = [("None", serial.PARITY_NONE), ("Odd", serial.PARITY_ODD),
            ("Even", serial.PARITY_EVEN), ("Mark", serial.PARITY_MARK),
            ("Space", serial.PARITY_SPACE)]
STOP_BITS = [("1", serial.STOPBITS_ONE), ("1.5", serial.STOPBITS_ONE_POINT_FIVE),
             ("2", serial.STOPBITS_TWO)]
FLOW_CONTROLS = [("None", "none"), ("RTS/CTS", "rtscts"), ("XON/XOFF", "xonxoff")]
# SSCOM 校验方式：0=None，1=modbusCRC16，2=ADD，3=XOR
CHECK_MODES = ["None", "CRC16(Modbus)", "ADD累加和", "XOR异或"]
CHECK_ENDS = [("末尾", 0), ("末尾-1", 1), ("末尾-2", 2), ("末尾-3", 3)]

# ANSI 转义序列（颜色/光标/标题等），用于“ANSI过滤”
ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]"      # CSI 序列，如 \x1b[1;32m
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"    # OSC 序列，如 \x1b]0;title\x07
    r"|\([0-9A-B]"                       # 字符集切换
    r"|[@-Z\\-_])")                      # 其他单字符转义

# Zephyr shell 提示符（ANSI 过滤时一并剥掉）
PROMPT_RE = re.compile(r"uart:~\$\s?")

MULTI_COUNT = 99                 # 多字符串条数（与 SSCOM 一致）
PANEL_WIDTH = 580                # 扩展面板宽度
RX_MAX_CHARS = 1_000_000         # 接收显示缓冲上限
FILE_CHUNK = 256                 # 发送文件分块（SSCOM 为每 256 字节）
FILE_CHUNK_DELAY = 1             # 分块延时 ms

# 接收区收发区分：颜色与前缀
RX_COLOR = "#008000"             # 接收：绿色
TX_COLOR = "#0000cc"             # 发送：蓝色
RX_PREFIX = "收← "               # 接收行前缀（时间戳模式）
TX_PREFIX = "发→ "               # 发送行前缀


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def make_led(color: str) -> QPixmap:
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 12, 12)
    p.end()
    return pm


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


APP_ICON = resource_path("icon.ico")


# ----------------------------------------------------------------------
# 串口接收线程
# ----------------------------------------------------------------------
class SerialReader(QThread):
    data_received = pyqtSignal(bytes)
    error_occurred = pyqtSignal(str)

    def __init__(self, ser, parent=None):
        super().__init__(parent)
        self._ser = ser
        self._running = True

    def run(self):
        while self._running:
            try:
                data = self._ser.read(4096)
                if data:
                    self.data_received.emit(data)
            except Exception as exc:
                if self._running:
                    self.error_occurred.emit(str(exc))
                break

    def stop(self):
        self._running = False


# ----------------------------------------------------------------------
# 更多串口设置对话框（仿 SSCOM Setup 对话框）
# ----------------------------------------------------------------------
class SetupDialog(QDialog):
    def __init__(self, parent, state):
        super().__init__(parent)
        self.setWindowTitle("Setup")
        self.setModal(True)
        g = QGroupBox("Settings")
        form = QFormLayout(g)

        self.cb_port = QComboBox()
        for i in range(parent.cb_port.count()):
            self.cb_port.addItem(parent.cb_port.itemText(i),
                                 parent.cb_port.itemData(i))
        if parent.cb_port.currentIndex() >= 0:
            self.cb_port.setCurrentIndex(parent.cb_port.currentIndex())

        self.cb_baud = QComboBox()
        self.cb_baud.setEditable(True)
        self.cb_baud.addItems(BAUD_RATES)
        self.cb_baud.setCurrentText(state["baud"])

        self.cb_databits = QComboBox()
        for t, _ in DATA_BITS:
            self.cb_databits.addItem(t)
        self.cb_databits.setCurrentText(state["databits"])

        self.cb_stopbits = QComboBox()
        for t, _ in STOP_BITS:
            self.cb_stopbits.addItem(t)
        self.cb_stopbits.setCurrentText(state["stopbits"])

        self.cb_parity = QComboBox()
        for t, _ in PARITIES:
            self.cb_parity.addItem(t)
        self.cb_parity.setCurrentText(state["parity"])

        self.cb_flow = QComboBox()
        for t, _ in FLOW_CONTROLS:
            self.cb_flow.addItem(t)
        self.cb_flow.setCurrentText(state["flow"])

        form.addRow("Port", self.cb_port)
        form.addRow("Baud rate", self.cb_baud)
        form.addRow("Data bits", self.cb_databits)
        form.addRow("Stop bits", self.cb_stopbits)
        form.addRow("Parity", self.cb_parity)
        form.addRow("Flow control", self.cb_flow)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(g)
        lay.addWidget(btns)


# ----------------------------------------------------------------------
# 多字符串面板（仿 SSCOM 扩展面板）
# ----------------------------------------------------------------------
class MultiStringPanel(QGroupBox):
    """99 条字符串：HEX 勾选 / 内容 / 注释(点击发送，双击内容改注释) / 顺序 / 延时"""

    def __init__(self, sender, parent=None):
        super().__init__("多条字符串发送", parent)
        # sender(hex_mode, text) -> bool  由主窗口提供（含加校验处理）
        self._sender = sender
        self.rows = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)

        top = QHBoxLayout()
        self.cb_cycle = QCheckBox("循环发送")
        self.cb_cycle.toggled.connect(self._on_cycle_toggled)
        btn_help = QPushButton("多条帮助")
        btn_help.clicked.connect(self._show_help)
        btn_import = QPushButton("导入ini")
        btn_import.clicked.connect(self.import_ini)
        btn_export = QPushButton("导出ini")
        btn_export.clicked.connect(self.export_ini)
        top.addWidget(self.cb_cycle)
        top.addWidget(btn_help)
        top.addWidget(btn_import)
        top.addWidget(btn_export)
        top.addStretch(1)
        lay.addLayout(top)

        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(1)
        for col, t in enumerate(("HEX", "字符串(双击改注释)", "点击发送", "顺序", "延时ms")):
            lab = QLabel(t)
            lab.setStyleSheet("color:#555;")
            grid.addWidget(lab, 0, col)

        for i in range(MULTI_COUNT):
            r = i + 1
            hex_cb = QCheckBox()
            edit = QLineEdit()
            edit.installEventFilter(self)
            btn = QPushButton(f"{r}无注释")
            btn.setStyleSheet("text-align:left;color:#7a5c00;")
            btn.clicked.connect(lambda _=False, idx=i: self.send_row(idx))
            sp_order = QSpinBox()
            sp_order.setRange(0, MULTI_COUNT)
            sp_order.setToolTip("循环顺序，0=不参与循环")
            sp_order.setFixedWidth(52)
            sp_delay = QSpinBox()
            sp_delay.setRange(20, 600000)
            sp_delay.setValue(1000)
            sp_delay.setFixedWidth(70)
            grid.addWidget(hex_cb, r, 0)
            grid.addWidget(edit, r, 1)
            grid.addWidget(btn, r, 2)
            grid.addWidget(sp_order, r, 3)
            grid.addWidget(sp_delay, r, 4)
            self.rows.append({
                "hex": hex_cb, "edit": edit, "btn": btn,
                "order": sp_order, "delay": sp_delay,
            })
        grid.setColumnStretch(1, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        lay.addWidget(scroll)

        self.cycle_timer = QTimer(self)
        self.cycle_timer.setSingleShot(True)
        self.cycle_timer.timeout.connect(self._cycle_step)
        self._cycle_list = []
        self._cycle_pos = 0

    # -- 双击字符串编辑注释 --
    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.MouseButtonDblClick:
            for i, row in enumerate(self.rows):
                if row["edit"] is obj:
                    text, ok = QInputDialog.getText(
                        self, "编辑注释", f"第 {i + 1} 条注释：",
                        text=row["btn"].text())
                    if ok and text.strip():
                        row["btn"].setText(text.strip())
                    return True
        return super().eventFilter(obj, ev)

    def send_row(self, i) -> bool:
        row = self.rows[i]
        text = row["edit"].text()
        if not text:
            return False
        return self._sender(row["hex"].isChecked(), text)

    # -- 循环发送：按“顺序”排序，逐条发送，每条后等待其“延时” --
    def _on_cycle_toggled(self, checked):
        if checked:
            self._cycle_list = sorted(
                (i for i, r in enumerate(self.rows) if r["order"].value() > 0),
                key=lambda i: self.rows[i]["order"].value())
            if not self._cycle_list:
                QMessageBox.information(
                    self, "提示", "没有可循环发送的字符串：\n请先把需要循环的条的“顺序”设为大于 0。")
                self.cb_cycle.setChecked(False)
                return
            self._cycle_pos = 0
            self._cycle_step()
        else:
            self.cycle_timer.stop()

    def _cycle_step(self):
        if not self.cb_cycle.isChecked() or not self._cycle_list:
            return
        idx = self._cycle_list[self._cycle_pos]
        self._cycle_pos = (self._cycle_pos + 1) % len(self._cycle_list)
        if self.send_row(idx):
            self.cycle_timer.start(self.rows[idx]["delay"].value())
        else:
            # 发送失败（如串口未打开）时停止循环
            self.cb_cycle.setChecked(False)

    def stop_cycle(self):
        self.cb_cycle.setChecked(False)

    # -- SSCOM 格式 ini 导入导出 --
    def import_ini(self, path=None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "导入字符串 ini", "", "ini 文件 (*.ini);;所有文件 (*.*)")
            if not path:
                return
        try:
            with open(path, "rb") as f:
                text = f.read().decode("gbk", errors="replace")
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        meta, data = {}, {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(";") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            if key.startswith("N10") and key[1:].isdigit() and len(key) == 4:
                idx = int(key[1:]) - 100          # N101 -> 第1条
                parts = val.split(",")
                if len(parts) >= 3:
                    meta[idx] = parts
            elif key.startswith("N") and key[1:].isdigit():
                idx = int(key[1:])
                parts = val.split(",", 1)
                if len(parts) == 2:
                    data[idx] = parts
        n = 0
        for idx in range(1, MULTI_COUNT + 1):
            row = self.rows[idx - 1]
            if idx in data:
                flag, content = data[idx]
                row["hex"].setChecked(flag.strip().upper() == "H")
                row["edit"].setText(content)
                n += 1
            if idx in meta:
                order, comment, delay = meta[idx][0], meta[idx][1], meta[idx][2]
                try:
                    row["order"].setValue(int(order))
                except ValueError:
                    pass
                if comment:
                    row["btn"].setText(comment)
                try:
                    row["delay"].setValue(max(20, int(delay)))
                except ValueError:
                    pass
        QMessageBox.information(self, "导入完成", f"已导入 {n} 条字符串。")

    def export_ini(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出字符串 ini", "strings.ini", "ini 文件 (*.ini)")
        if not path:
            return
        lines = [";SerialTool 多字符串导出（兼容 SSCOM 格式）"]
        for i, row in enumerate(self.rows):
            idx = i + 1
            comment = row["btn"].text()
            lines.append(f"N{100 + idx}={row['order'].value()},{comment},{row['delay'].value()}")
            flag = "H" if row["hex"].isChecked() else "A"
            lines.append(f"N{idx}={flag},{row['edit'].text()}")
            lines.append("")
        try:
            with open(path, "wb") as f:
                f.write("\r\n".join(lines).encode("gbk", errors="replace"))
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出完成", f"已保存到 {path}")

    def _show_help(self):
        QMessageBox.information(self, "多条帮助",
                                "1. 点击右侧注释按钮发送该条字符串；\n"
                                "2. 双击字符串输入框可修改注释；\n"
                                "3. 勾选 HEX 表示该条按十六进制发送；\n"
                                "4. “顺序”>0 的条参与循环发送，按顺序号从小到大发送，\n"
                                "   每条发送后等待其“延时”毫秒再发下一条；\n"
                                "5. 发送内容会套用主界面的“加校验”设置。")


# ----------------------------------------------------------------------
# 主窗口
# ----------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, icon=None):
        super().__init__()
        self.setWindowTitle("SerialTool 串口调试工具")
        if icon is not None:
            self.setWindowIcon(icon)

        self.ser = None
        self.reader = None
        self.rx_count = 0
        self.tx_count = 0
        self._rx_file = None

        # 串口参数（数据位/停止位/校验/流控 在 Setup 对话框中修改）
        self.port_state = {"databits": "8", "stopbits": "1",
                           "parity": "None", "flow": "None"}

        # 分包显示状态
        self._pkt_buf = bytearray()
        self._pkt_time = None
        self.pkt_timer = QTimer(self)
        self.pkt_timer.setSingleShot(True)
        self.pkt_timer.setTimerType(Qt.PreciseTimer)   # ms 级精度分包
        self.pkt_timer.timeout.connect(self._flush_packet)

        # 定时发送
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self.send_main)

        # 文件发送
        self.file_timer = QTimer(self)
        self.file_timer.timeout.connect(self._file_send_tick)
        self._file_bytes = b""
        self._file_pos = 0

        self._build_ui()
        self._build_menus()
        self._build_statusbar()
        self.refresh_ports()
        self._load_settings()
        self.resize(792, 600)

        # 启动自动打开串口
        if self.act_auto_open.isChecked() and self.cb_port.currentData():
            QTimer.singleShot(300, self._toggle_port)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(3)

        # 接收区 + 扩展面板
        self.rx_edit = QPlainTextEdit()
        self.rx_edit.setReadOnly(True)
        self.rx_edit.setMaximumBlockCount(300000)
        self.rx_edit.setMinimumWidth(60)     # 允许窗口横向缩得更窄
        self.multi_panel = MultiStringPanel(self._send_payload)
        self.multi_panel.setVisible(False)
        self.multi_panel.setMinimumWidth(420)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.rx_edit)
        self.splitter.addWidget(self.multi_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        lay.addWidget(self.splitter, 1)

        # 工具行
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self.btn_clear_rx = QPushButton("清除窗口")
        self.btn_clear_rx.clicked.connect(self.rx_edit.clear)
        self.btn_open_file = QPushButton("打开文件")
        self.btn_open_file.clicked.connect(self.load_file_to_send)
        self.btn_send_file = QPushButton("发送文件")
        self.btn_send_file.clicked.connect(self.send_file)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.clicked.connect(self.stop_file_send)
        self.btn_clear_tx = QPushButton("清发送区")
        self.btn_clear_tx.clicked.connect(self.tx_edit_clear)
        self.cb_topmost = QCheckBox("最前")
        self.cb_topmost.toggled.connect(self._on_topmost)
        self.btn_save_cfg = QPushButton("保存参数")
        self.btn_save_cfg.clicked.connect(self._save_settings)
        self.btn_extend = QPushButton("扩展")
        self.btn_extend.setCheckable(True)
        self.btn_extend.toggled.connect(self._toggle_extend)
        for w in (self.btn_clear_rx, self.btn_open_file):
            bar.addWidget(w)
        bar.addStretch(1)
        for w in (self.btn_send_file, self.btn_stop, self.btn_clear_tx,
                  self.cb_topmost, self.btn_save_cfg, self.btn_extend):
            bar.addWidget(w)
        lay.addWidget(self._wrap_hscroll(bar))

        # 端口行
        row1 = QHBoxLayout()
        row1.setSpacing(4)
        row1.addWidget(QLabel("端口号"))
        self.cb_port = QComboBox()
        self.cb_port.setMinimumWidth(170)
        self.cb_port.currentIndexChanged.connect(self._update_status_port)
        row1.addWidget(self.cb_port)
        self.cb_hex_show = QCheckBox("HEX显示")
        self.cb_ansi = QCheckBox("ANSI过滤")
        self.cb_ansi.setChecked(True)
        self.cb_ansi.setToolTip("过滤设备 shell 的 ANSI 颜色/光标转义序列和 uart:~$ 提示符")
        self.btn_save_rx = QPushButton("保存数据")
        self.btn_save_rx.clicked.connect(self.save_rx_data)
        self.cb_rx_to_file = QCheckBox("接收数据到文件")
        self.cb_rx_to_file.toggled.connect(self._on_rx_to_file)
        self.cb_hex_send = QCheckBox("HEX发送")
        self.cb_show_tx = QCheckBox("显示发送")
        self.cb_show_tx.setChecked(True)
        self.cb_show_tx.setToolTip("在接收窗口回显发送内容（蓝色“发→”），与接收（绿色“收←”）区分")
        self.cb_auto_send = QCheckBox("定时发送:")
        self.cb_auto_send.toggled.connect(self._on_auto_send)
        self.spin_auto = QSpinBox()
        self.spin_auto.setRange(10, 3_600_000)
        self.spin_auto.setValue(1000)
        self.spin_auto.setFixedWidth(70)
        self.cb_newline = QCheckBox("加回车换行")
        self.cb_newline.setChecked(True)
        row1.addWidget(self.cb_hex_show)
        row1.addWidget(self.cb_ansi)
        row1.addWidget(self.btn_save_rx)
        row1.addWidget(self.cb_rx_to_file)
        row1.addWidget(self.cb_hex_send)
        row1.addWidget(self.cb_show_tx)
        row1.addWidget(self.cb_auto_send)
        row1.addWidget(self.spin_auto)
        row1.addWidget(QLabel("ms/次"))
        row1.addWidget(self.cb_newline)
        row1.addStretch(1)
        lay.addWidget(self._wrap_hscroll(row1))

        # 设置行 + 底部（左栏 / 发送框）
        bottom = QHBoxLayout()
        bottom.setSpacing(4)

        left = QVBoxLayout()
        left.setSpacing(3)
        row_open = QHBoxLayout()
        row_open.setSpacing(3)
        self.led_red = make_led("#c0392b")
        self.led_green = make_led("#27ae60")
        self.btn_open = QPushButton(" 打开串口")
        self.btn_open.setIcon(self.__class__._icon(self.led_red))
        self.btn_open.setMinimumHeight(26)
        self.btn_open.clicked.connect(self._toggle_port)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setFixedWidth(44)
        self.btn_refresh.clicked.connect(self.refresh_ports)
        self.btn_setup = QPushButton("更多串口设置")
        self.btn_setup.clicked.connect(self.show_setup_dialog)
        row_open.addWidget(self.btn_open)
        row_open.addWidget(self.btn_refresh)
        row_open.addWidget(self.btn_setup)
        left.addLayout(row_open)

        row_baud = QHBoxLayout()
        row_baud.setSpacing(3)
        self.cb_rts = QCheckBox("RTS")
        self.cb_rts.toggled.connect(self._apply_dtr_rts)
        self.cb_dtr = QCheckBox("DTR")
        self.cb_dtr.setChecked(True)
        self.cb_dtr.toggled.connect(self._apply_dtr_rts)
        self.cb_baud = QComboBox()
        self.cb_baud.setEditable(True)
        self.cb_baud.addItems(BAUD_RATES)
        self.cb_baud.setCurrentText("9600")
        self.cb_baud.currentTextChanged.connect(self._update_status_port)
        row_baud.addWidget(self.cb_rts)
        row_baud.addWidget(self.cb_dtr)
        row_baud.addWidget(QLabel("波特率:"))
        row_baud.addWidget(self.cb_baud, 1)
        left.addLayout(row_baud)

        lab_info = QLabel("SerialTool 串口调试工具")
        lab_info.setStyleSheet("color:#808080;")
        left.addWidget(lab_info)

        self.btn_send = QPushButton("发 送")
        self.btn_send.setMinimumHeight(40)
        self.btn_send.clicked.connect(self.send_main)
        left.addWidget(self.btn_send)
        left.addStretch(1)

        leftw = QWidget()
        leftw.setLayout(left)
        leftw.setFixedWidth(240)
        bottom.addWidget(leftw, 0, Qt.AlignTop)

        right = QVBoxLayout()
        right.setSpacing(3)
        row_ts = QHBoxLayout()
        row_ts.setSpacing(4)
        self.cb_timestamp = QCheckBox("加时间戳和分包显示")
        self.cb_timestamp.setChecked(True)
        self.cb_timestamp.toggled.connect(self._on_timestamp_toggled)
        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(1, 10000)
        self.spin_timeout.setValue(20)
        self.spin_timeout.setFixedWidth(58)
        self.spin_check_start = QSpinBox()
        self.spin_check_start.setRange(1, 9999)
        self.spin_check_start.setValue(1)
        self.spin_check_start.setFixedWidth(50)
        self.cb_check_end = QComboBox()
        for t, _ in CHECK_ENDS:
            self.cb_check_end.addItem(t)
        self.cb_check = QComboBox()
        self.cb_check.addItems(CHECK_MODES)
        row_ts.addWidget(self.cb_timestamp)
        row_ts.addWidget(QLabel("超时时间:"))
        row_ts.addWidget(self.spin_timeout)
        row_ts.addWidget(QLabel("ms"))
        row_ts.addWidget(QLabel("第"))
        row_ts.addWidget(self.spin_check_start)
        row_ts.addWidget(QLabel("字节 至"))
        row_ts.addWidget(self.cb_check_end)
        row_ts.addWidget(QLabel("加校验"))
        row_ts.addWidget(self.cb_check)
        row_ts.addStretch(1)
        right.addWidget(self._wrap_hscroll(row_ts))

        self.tx_edit = QPlainTextEdit()
        self.tx_edit.setMinimumHeight(64)
        self.tx_edit.setMinimumWidth(80)    # 允许窗口横向缩得更窄
        right.addWidget(self.tx_edit, 1)
        bottom.addLayout(right, 1)
        lay.addLayout(bottom)

        self.setCentralWidget(central)

    @staticmethod
    def _wrap_hscroll(layout):
        """把一行控件包进横向滚动区：窗口变窄时该行出现横向滚动条，
        而不是撑住窗口。高度 = 行内容高度 + 细滚动条高度，保证不压行。"""
        layout.setContentsMargins(0, 2, 0, 2)
        inner = QWidget()
        inner.setLayout(layout)
        sa = QScrollArea()
        sa.setWidget(inner)
        sa.setWidgetResizable(True)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        sa.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setFrameShape(QScrollArea.NoFrame)
        sa.setStyleSheet("QScrollBar:horizontal{height:12px;}")
        sa.setMinimumWidth(40)
        sa.setFixedHeight(inner.sizeHint().height() + 14)
        return sa

    @staticmethod
    def _icon(pixmap):
        from PyQt5.QtGui import QIcon
        return QIcon(pixmap)

    def tx_edit_clear(self):
        self.tx_edit.clear()

    def _build_menus(self):
        mb = self.menuBar()

        m_port = mb.addMenu("通讯端口(&P)")
        act = QAction("刷新串口列表", self)
        act.setShortcut("F5")
        act.triggered.connect(self.refresh_ports)
        m_port.addAction(act)
        self.act_open = QAction("打开串口", self)
        self.act_open.triggered.connect(self._toggle_port)
        m_port.addAction(self.act_open)
        m_port.addSeparator()
        act = QAction("退出", self)
        act.triggered.connect(self.close)
        m_port.addAction(act)

        m_set = mb.addMenu("串口设置(&S)")
        act = QAction("更多串口设置…", self)
        act.triggered.connect(self.show_setup_dialog)
        m_set.addAction(act)
        act = QAction("保存参数", self)
        act.triggered.connect(self._save_settings)
        m_set.addAction(act)
        self.act_auto_open = QAction("启动时自动打开串口", self)
        self.act_auto_open.setCheckable(True)
        m_set.addAction(self.act_auto_open)

        m_view = mb.addMenu("显示(&V)")
        self._add_sync_action(m_view, "HEX显示", self.cb_hex_show)
        self._add_sync_action(m_view, "ANSI过滤", self.cb_ansi)
        self._add_sync_action(m_view, "显示发送", self.cb_show_tx)
        self._add_sync_action(m_view, "加时间戳和分包显示", self.cb_timestamp)
        m_view.addSeparator()
        act = QAction("清除窗口", self)
        act.triggered.connect(self.rx_edit.clear)
        m_view.addAction(act)
        act = QAction("保存数据…", self)
        act.triggered.connect(self.save_rx_data)
        m_view.addAction(act)
        self._add_sync_action(m_view, "接收数据到文件", self.cb_rx_to_file)
        m_view.addSeparator()
        act = QAction("计数清零", self)
        act.triggered.connect(self._reset_counters)
        m_view.addAction(act)

        m_send = mb.addMenu("发送(&T)")
        act = QAction("发送", self)
        act.setShortcut("Ctrl+Return")
        act.triggered.connect(self.send_main)
        m_send.addAction(act)
        act = QAction("发送文件…", self)
        act.triggered.connect(self.send_file)
        m_send.addAction(act)
        act = QAction("停止发送文件", self)
        act.triggered.connect(self.stop_file_send)
        m_send.addAction(act)
        act = QAction("打开文件到发送区…", self)
        act.triggered.connect(self.load_file_to_send)
        m_send.addAction(act)
        act = QAction("清发送区", self)
        act.triggered.connect(self.tx_edit_clear)
        m_send.addAction(act)
        m_send.addSeparator()
        self._add_sync_action(m_send, "HEX发送", self.cb_hex_send)
        self._add_sync_action(m_send, "定时发送", self.cb_auto_send)
        self._add_sync_action(m_send, "加回车换行", self.cb_newline)

        m_multi = mb.addMenu("多字符串(&M)")
        act = QAction("扩展/隐藏面板", self)
        act.triggered.connect(lambda: self.btn_extend.toggle())
        m_multi.addAction(act)
        self._add_sync_action(m_multi, "循环发送", self.multi_panel.cb_cycle)
        m_multi.addSeparator()
        act = QAction("导入ini…", self)
        act.triggered.connect(self.multi_panel.import_ini)
        m_multi.addAction(act)
        act = QAction("导出ini…", self)
        act.triggered.connect(self.multi_panel.export_ini)
        m_multi.addAction(act)

        m_help = mb.addMenu("帮助(&H)")
        act = QAction("关于 SerialTool", self)
        act.triggered.connect(self._show_about)
        m_help.addAction(act)

    def _add_sync_action(self, menu, text, checkbox):
        act = QAction(text, self)
        act.setCheckable(True)
        act.setChecked(checkbox.isChecked())
        act.toggled.connect(checkbox.setChecked)
        checkbox.toggled.connect(act.setChecked)
        menu.addAction(act)
        return act

    def _build_statusbar(self):
        sb = self.statusBar()
        sb.addWidget(QLabel("SerialTool"), 0)
        self.st_s = QLabel("S:0")
        self.st_r = QLabel("R:0")
        self.st_port = QLabel("串口已关闭")
        sb.addWidget(self.st_s, 0)
        sb.addWidget(self.st_r, 0)
        sb.addWidget(self.st_port, 1)
        btn = QPushButton("计数清零")
        btn.setFlat(True)
        btn.clicked.connect(self._reset_counters)
        sb.addPermanentWidget(btn)

    def _show_about(self):
        QMessageBox.about(self, "关于 SerialTool",
                          "SerialTool V1.0\n\n"
                          "仿 SSCOM V5.13.1 界面与功能的串口调试工具（仅串口）。\n"
                          "基于 Python3 + PyQt5 + pyserial 实现。\n"
                          "多字符串 ini 与 SSCOM 格式兼容。")

    # ------------------------------------------------------------------
    # 串口管理
    # ------------------------------------------------------------------
    def refresh_ports(self):
        current = self.cb_port.currentData()
        self.cb_port.clear()
        ports = list(list_ports.comports())
        for p in ports:
            desc = p.description.split(" (")[0]
            self.cb_port.addItem(f"{p.device} {desc}", p.device)
        if current:
            idx = self.cb_port.findData(current)
            if idx >= 0:
                self.cb_port.setCurrentIndex(idx)
        if not ports:
            self.cb_port.addItem("（未检测到串口）", None)
        self._update_status_port()

    def _toggle_port(self):
        if self.ser and self.ser.is_open:
            self._close_port()
        else:
            self._open_port()

    def _open_port(self):
        device = self.cb_port.currentData()
        if not device:
            QMessageBox.warning(self, "提示", "没有可用的串口，请插入设备后点击【刷新】。")
            return
        try:
            baud = int(self.cb_baud.currentText().strip())
            if not (50 <= baud <= 4_000_000):
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "提示", "波特率必须是 50 ~ 4000000 之间的整数。")
            return
        st = self.port_state
        try:
            self.ser = serial.Serial(
                port=device, baudrate=baud,
                bytesize=dict(DATA_BITS)[st["databits"]],
                parity=dict(PARITIES)[st["parity"]],
                stopbits=dict(STOP_BITS)[st["stopbits"]],
                xonxoff=(st["flow"] == "xonxoff"),
                rtscts=(st["flow"] == "rtscts"),
                timeout=0.05, write_timeout=2)
            self.ser.dtr = self.cb_dtr.isChecked()
            self.ser.rts = self.cb_rts.isChecked()
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", f"无法打开 {device}：\n{exc}")
            self.ser = None
            return

        self.reader = SerialReader(self.ser, self)
        self.reader.data_received.connect(self._on_data)
        self.reader.error_occurred.connect(self._on_serial_error)
        self.reader.start()
        self._set_port_ui(True)

    def _close_port(self):
        self.cb_auto_send.setChecked(False)
        self.multi_panel.stop_cycle()
        self.file_timer.stop()
        if self.reader:
            self.reader.stop()
            self.reader.wait(1000)
            self.reader = None
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self._set_port_ui(False)

    def _set_port_ui(self, opened):
        self.btn_open.setText(" 关闭串口" if opened else " 打开串口")
        self.btn_open.setIcon(self._icon(self.led_green if opened else self.led_red))
        self.act_open.setText("关闭串口" if opened else "打开串口")
        for w in (self.cb_port, self.btn_refresh, self.btn_setup, self.cb_baud):
            w.setEnabled(not opened)
        self._update_status_port()

    def _update_status_port(self):
        opened = bool(self.ser and self.ser.is_open)
        device = self.cb_port.currentData() or "----"
        st = self.port_state
        self.st_port.setText(
            f"{device} {'已打开' if opened else '已关闭'} "
            f"{self.cb_baud.currentText()}bps,{st['databits']},{st['stopbits']},"
            f"{st['parity']},{st['flow']}")

    def _apply_dtr_rts(self):
        if self.ser and self.ser.is_open:
            try:
                self.ser.dtr = self.cb_dtr.isChecked()
                self.ser.rts = self.cb_rts.isChecked()
            except Exception:
                pass

    def _on_serial_error(self, msg):
        if self.ser and self.ser.is_open:
            self._close_port()
            QMessageBox.warning(self, "串口错误",
                                f"串口通信中断（设备可能已拔出）：\n{msg}")

    def show_setup_dialog(self):
        st = dict(self.port_state)
        st["baud"] = self.cb_baud.currentText()
        dlg = SetupDialog(self, st)
        if dlg.exec_() == QDialog.Accepted:
            self.port_state.update({
                "databits": dlg.cb_databits.currentText(),
                "stopbits": dlg.cb_stopbits.currentText(),
                "parity": dlg.cb_parity.currentText(),
                "flow": dlg.cb_flow.currentText(),
            })
            self.cb_baud.setCurrentText(dlg.cb_baud.currentText())
            idx = self.cb_port.findData(dlg.cb_port.currentData())
            if idx >= 0:
                self.cb_port.setCurrentIndex(idx)
            self._update_status_port()

    # ------------------------------------------------------------------
    # 接收
    # ------------------------------------------------------------------
    def _on_data(self, data: bytes):
        self.rx_count += len(data)
        self._update_counters()
        if self.cb_timestamp.isChecked():
            if not self._pkt_buf:
                self._pkt_time = datetime.datetime.now()
            self._pkt_buf.extend(data)
            self.pkt_timer.start(self.spin_timeout.value())   # 分包超时
        else:
            # 不分包模式：文件保存原始字节流（无时间戳）
            if self._rx_file:
                self._file_write_raw(data)
            self._display(bytes(data), None)

    def _flush_packet(self):
        if self._pkt_buf:
            buf = bytes(self._pkt_buf)
            ts = self._pkt_time
            self._pkt_buf.clear()
            # 分包模式：文件按包写入 ms 级时间戳
            if self._rx_file:
                self._file_write_packet(buf, ts)
            self._display(buf, ts)

    def _file_write_raw(self, data: bytes):
        try:
            self._rx_file.write(data)
            self._rx_file.flush()
        except Exception:
            pass

    def _file_write_packet(self, buf: bytes, ts):
        """接收包逐行写入：[年-月-日 时:分:秒.毫秒] 收← 内容（HEX 或 GBK 文本）"""
        stamp = f"[{ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] {RX_PREFIX}"
        text = self._prefix_lines(self._format_rx(buf), stamp)
        try:
            self._rx_file.write(
                text.replace("\n", "\r\n").encode("gbk", errors="replace"))
            self._rx_file.flush()
        except Exception:
            pass

    def _file_write_tx(self, payload: bytes, ts):
        """发送内容写入保存文件，带“发→”前缀，与接收区分。"""
        stamp = f"[{ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] {TX_PREFIX}"
        text = self._prefix_lines(self._format_rx(payload), stamp)
        try:
            self._rx_file.write(
                text.replace("\n", "\r\n").encode("gbk", errors="replace"))
            self._rx_file.flush()
        except Exception:
            pass

    def _format_rx(self, data: bytes) -> str:
        """接收内容格式化：HEX 或 GBK 文本，可选 ANSI 过滤。"""
        if self.cb_hex_show.isChecked():
            return " ".join(f"{b:02X}" for b in data) + " "
        text = data.decode("gbk", errors="replace")
        if self.cb_ansi.isChecked():
            text = ANSI_RE.sub("", text)
            text = PROMPT_RE.sub("", text)   # 剥掉 shell 提示符 uart:~$
        return text

    @staticmethod
    def _prefix_lines(text: str, stamp: str) -> str:
        """给（可能多行的）文本逐行加 时间戳+方向 前缀，空行跳过。"""
        return "".join(f"{stamp}{ln}\n"
                       for ln in (l.rstrip("\r") for l in text.split("\n")) if ln)

    def _display(self, data: bytes, ts, is_tx=False):
        """把数据显示到接收窗口，收/发用颜色和前缀区分。

        is_tx=False：接收，绿色，时间戳模式下前缀“收←”；
        is_tx=True ：发送回显，蓝色，前缀“发→”，单独成行。
        时间戳逐行添加：一个分包里的每行日志都有自己的时间戳。
        """
        text = self._format_rx(data)
        if is_tx:
            stamp = (f"[{ts.strftime('%H:%M:%S.%f')[:-3]}] {TX_PREFIX}"
                     if ts is not None else TX_PREFIX)
            text = self._prefix_lines(text, stamp)
        elif ts is not None:
            stamp = f"[{ts.strftime('%H:%M:%S.%f')[:-3]}] {RX_PREFIX}"
            text = self._prefix_lines(text, stamp)
        cur = self.rx_edit.textCursor()
        cur.movePosition(QTextCursor.End)
        self.rx_edit.setTextCursor(cur)
        fmt = QTextCharFormat()
        fmt.setForeground(QBrush(QColor(TX_COLOR if is_tx else RX_COLOR)))
        cur.insertText(text, fmt)
        if self.rx_edit.document().characterCount() > RX_MAX_CHARS:
            cur.movePosition(QTextCursor.Start)
            cur.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor,
                             RX_MAX_CHARS // 4)
            cur.removeSelectedText()
        bar = self.rx_edit.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_timestamp_toggled(self, checked):
        if not checked and self._pkt_buf:
            self.pkt_timer.stop()
            self._flush_packet()

    def save_rx_data(self):
        if not self.rx_edit.toPlainText():
            QMessageBox.information(self, "提示", "接收区暂无数据。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存接收数据",
            datetime.datetime.now().strftime("Received-%Y%m%d-%H%M%S.txt"),
            "文本文件 (*.txt);;所有文件 (*.*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write(self.rx_edit.toPlainText())
            self.statusBar().showMessage(f"已保存到 {path}", 5000)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))

    def _on_rx_to_file(self, checked):
        if checked:
            path, _ = QFileDialog.getSaveFileName(
                self, "选择接收保存文件",
                datetime.datetime.now().strftime("R-%Y%m%d-%H%M%S.dat"),
                "数据文件 (*.dat *.bin *.txt);;所有文件 (*.*)")
            if not path:
                self.cb_rx_to_file.setChecked(False)
                return
            try:
                self._rx_file = open(path, "ab")
            except Exception as exc:
                QMessageBox.critical(self, "打开失败", str(exc))
                self.cb_rx_to_file.setChecked(False)
        else:
            if self._rx_file:
                try:
                    self._rx_file.close()
                except Exception:
                    pass
                self._rx_file = None

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def _build_payload(self, text: str, hex_mode: bool):
        if hex_mode:
            s = "".join(text.split())
            if not s:
                return b""
            if len(s) % 2:
                QMessageBox.warning(self, "HEX 格式错误",
                                    "HEX 数据长度必须是偶数（每字节两位十六进制）。")
                return None
            try:
                return bytes.fromhex(s)
            except ValueError:
                QMessageBox.warning(self, "HEX 格式错误",
                                    "HEX 数据只能包含 0-9、A-F 和空格。")
                return None
        return text.encode("gbk", errors="replace")

    def _apply_check(self, payload: bytes) -> bytes:
        """按“第N字节至末尾-K字节”范围计算校验并追加。"""
        mode = self.cb_check.currentIndex()
        if mode == 0 or not payload:
            return payload
        start = self.spin_check_start.value() - 1
        drop = dict(CHECK_ENDS)[self.cb_check_end.currentText()]
        end = len(payload) - drop
        rng = payload[start:end] if 0 <= start < end <= len(payload) else payload
        if not rng:
            rng = payload
        if mode == 1:                       # CRC16(Modbus)，低字节在前
            crc = crc16_modbus(rng)
            return payload + bytes((crc & 0xFF, crc >> 8))
        if mode == 2:                       # ADD 累加和
            return payload + bytes((sum(rng) & 0xFF,))
        return payload + bytes((__import__("functools").reduce(
            lambda a, b: a ^ b, rng, 0),))  # XOR 异或

    def _send_payload(self, hex_mode: bool, text: str,
                      add_newline: bool = False) -> bool:
        payload = self._build_payload(text, hex_mode)
        if payload is None:
            return False
        if add_newline and self.cb_newline.isChecked():
            payload += b"\r\n"
        payload = self._apply_check(payload)
        return self._write(payload)

    def _write(self, payload: bytes, echo: bool = True) -> bool:
        if not (self.ser and self.ser.is_open):
            QMessageBox.warning(self, "提示", "请先打开串口。")
            return False
        if not payload:
            return False
        try:
            n = self.ser.write(payload)
            self.tx_count += n
            self._update_counters()
            # 发送回显（蓝色“发→”），与接收数据区分
            if echo and self.cb_show_tx.isChecked():
                now = datetime.datetime.now()
                ts = now if self.cb_timestamp.isChecked() else None
                self._display(payload, ts, is_tx=True)
                # 同步写入“接收数据到文件”，带“发→”方向标识
                if self._rx_file:
                    self._file_write_tx(payload, now)
            return True
        except Exception as exc:
            self._close_port()
            QMessageBox.critical(self, "发送失败", str(exc))
            return False

    def send_main(self):
        text = self.tx_edit.toPlainText()
        if not text:
            self.cb_auto_send.setChecked(False)
            return
        ok = self._send_payload(self.cb_hex_send.isChecked(), text,
                                add_newline=True)
        if not ok and self.cb_auto_send.isChecked():
            self.cb_auto_send.setChecked(False)

    def _on_auto_send(self, checked):
        if checked:
            if not (self.ser and self.ser.is_open):
                QMessageBox.warning(self, "提示", "请先打开串口。")
                self.cb_auto_send.setChecked(False)
                return
            self.auto_timer.start(self.spin_auto.value())
        else:
            self.auto_timer.stop()

    def load_file_to_send(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开文件到发送区")
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as exc:
            QMessageBox.critical(self, "读取失败", str(exc))
            return
        if self.cb_hex_send.isChecked():
            self.tx_edit.setPlainText(" ".join(f"{b:02X}" for b in data))
        else:
            self.tx_edit.setPlainText(data.decode("gbk", errors="replace"))

    def send_file(self):
        if not (self.ser and self.ser.is_open):
            QMessageBox.warning(self, "提示", "请先打开串口。")
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择要发送的文件")
        if not path:
            return
        try:
            with open(path, "rb") as f:
                self._file_bytes = f.read()
        except Exception as exc:
            QMessageBox.critical(self, "读取失败", str(exc))
            return
        self._file_pos = 0
        self.statusBar().showMessage(
            f"发送文件 {os.path.basename(path)}（{len(self._file_bytes)} 字节）…")
        self.file_timer.start(FILE_CHUNK_DELAY)

    def _file_send_tick(self):
        chunk = self._file_bytes[self._file_pos:self._file_pos + FILE_CHUNK]
        if not chunk:
            self.file_timer.stop()
            return
        if not self._write(chunk, echo=False):   # 文件分块发送不回显，避免刷屏
            self.file_timer.stop()
            return
        self._file_pos += len(chunk)
        total = len(self._file_bytes)
        if self._file_pos >= total:
            self.file_timer.stop()
            self.statusBar().showMessage(f"文件发送完成（{total} 字节）", 5000)
        else:
            self.statusBar().showMessage(f"文件发送中 {self._file_pos}/{total} 字节")

    def stop_file_send(self):
        if self.file_timer.isActive():
            self.file_timer.stop()
            self.statusBar().showMessage("已停止文件发送", 3000)

    # ------------------------------------------------------------------
    # 其他
    # ------------------------------------------------------------------
    def _on_topmost(self, checked):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, checked)
        self.show()

    def _sync_extend_splitter(self, checked):
        if checked:
            total = max(1, self.splitter.width())
            panel = min(PANEL_WIDTH, max(420, total // 3))
            self.splitter.setSizes([max(1, total - panel), panel])
        else:
            self.splitter.setSizes([max(1, self.splitter.width()), 0])

    def _toggle_extend(self, checked):
        self.multi_panel.setVisible(checked)
        self.btn_extend.setText("隐藏" if checked else "扩展")
        if self.isMaximized() or self.isFullScreen():
            QTimer.singleShot(0, lambda c=checked: self._sync_extend_splitter(c))
            return
        dh = self.height()
        if checked:
            self.resize(self.width() + PANEL_WIDTH, dh)
        else:
            self.resize(max(500, self.width() - PANEL_WIDTH), dh)
        QTimer.singleShot(0, lambda c=checked: self._sync_extend_splitter(c))

    def _update_counters(self):
        self.st_s.setText(f"S:{self.tx_count}")
        self.st_r.setText(f"R:{self.rx_count}")

    def _reset_counters(self):
        self.rx_count = self.tx_count = 0
        self._update_counters()

    # ------------------------------------------------------------------
    # 参数保存 / 恢复
    # ------------------------------------------------------------------
    def _ini_path(self):
        if getattr(sys, "frozen", False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, "serialtool.ini")

    def _save_settings(self):
        s = QSettings(self._ini_path(), QSettings.IniFormat)
        s.setValue("port", self.cb_port.currentData() or "")
        s.setValue("baud", self.cb_baud.currentText())
        for k, v in self.port_state.items():
            s.setValue(k, v)
        s.setValue("dtr", self.cb_dtr.isChecked())
        s.setValue("rts", self.cb_rts.isChecked())
        s.setValue("hex_send", self.cb_hex_send.isChecked())
        s.setValue("show_tx", self.cb_show_tx.isChecked())
        s.setValue("hex_show", self.cb_hex_show.isChecked())
        s.setValue("ansi_filter", self.cb_ansi.isChecked())
        s.setValue("timestamp", self.cb_timestamp.isChecked())
        s.setValue("timeout", self.spin_timeout.value())
        s.setValue("newline", self.cb_newline.isChecked())
        s.setValue("auto_interval", self.spin_auto.value())
        s.setValue("check_mode", self.cb_check.currentIndex())
        s.setValue("check_start", self.spin_check_start.value())
        s.setValue("check_end", self.cb_check_end.currentIndex())
        s.setValue("topmost", self.cb_topmost.isChecked())
        s.setValue("auto_open", self.act_auto_open.isChecked())
        s.setValue("extended", self.btn_extend.isChecked())
        s.setValue("geometry", self.saveGeometry())
        s.setValue("send_text", self.tx_edit.toPlainText())
        s.beginWriteArray("multi")
        for i, row in enumerate(self.multi_panel.rows):
            s.setArrayIndex(i)
            s.setValue("hex", row["hex"].isChecked())
            s.setValue("text", row["edit"].text())
            s.setValue("comment", row["btn"].text())
            s.setValue("order", row["order"].value())
            s.setValue("delay", row["delay"].value())
        s.endArray()
        self.statusBar().showMessage("参数已保存", 2000)

    def _load_settings(self):
        path = self._ini_path()
        if not os.path.exists(path):
            return
        s = QSettings(path, QSettings.IniFormat)

        def b(key, default=False):
            v = s.value(key, default)
            if isinstance(v, str):
                return v.lower() in ("true", "1", "yes")
            return bool(v)

        saved_port = s.value("port", "")
        self.cb_baud.setCurrentText(s.value("baud", "9600"))
        for k in ("databits", "stopbits", "parity", "flow"):
            v = s.value(k)
            if v:
                self.port_state[k] = v
        self.cb_dtr.setChecked(b("dtr", True))
        self.cb_rts.setChecked(b("rts"))
        self.cb_hex_send.setChecked(b("hex_send"))
        self.cb_show_tx.setChecked(b("show_tx", True))
        self.cb_hex_show.setChecked(b("hex_show"))
        self.cb_ansi.setChecked(b("ansi_filter", True))
        self.cb_timestamp.setChecked(b("timestamp", True))
        self.spin_timeout.setValue(int(s.value("timeout", 20)))
        self.cb_newline.setChecked(b("newline", True))
        self.spin_auto.setValue(int(s.value("auto_interval", 1000)))
        self.cb_check.setCurrentIndex(int(s.value("check_mode", 0)))
        self.spin_check_start.setValue(int(s.value("check_start", 1)))
        self.cb_check_end.setCurrentIndex(int(s.value("check_end", 0)))
        self.act_auto_open.setChecked(b("auto_open"))
        self.tx_edit.setPlainText(s.value("send_text", ""))
        geo = s.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        if saved_port:
            idx = self.cb_port.findData(saved_port)
            if idx >= 0:
                self.cb_port.setCurrentIndex(idx)
        n = s.beginReadArray("multi")
        for i in range(min(n, len(self.multi_panel.rows))):
            s.setArrayIndex(i)
            row = self.multi_panel.rows[i]
            row["hex"].setChecked(b("hex"))
            row["edit"].setText(s.value("text", ""))
            comment = s.value("comment", "")
            if comment:
                row["btn"].setText(comment)
            row["order"].setValue(int(s.value("order", 0)))
            row["delay"].setValue(int(s.value("delay", 1000)))
        s.endArray()
        if b("extended"):
            self.btn_extend.setChecked(True)
        if b("topmost"):
            self.cb_topmost.setChecked(True)
        self._update_status_port()

    def closeEvent(self, event):
        try:
            self._save_settings()
        except Exception:
            pass
        try:
            self._close_port()
        except Exception:
            pass
        if self._rx_file:
            try:
                self._rx_file.close()
            except Exception:
                pass
        event.accept()


# ----------------------------------------------------------------------
def main():
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("com.tools.SerialTool")
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setStyle("windowsvista")
    app.setFont(QFont("宋体", 9))
    icon = QIcon(APP_ICON)
    app.setApplicationName("SerialTool")
    app.setWindowIcon(icon)
    w = MainWindow(icon)
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
