#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SerialTool 功能自测脚本（离屏运行，不依赖真实串口）。

用法:  QT_QPA_PLATFORM=offscreen python test_serial_tool.py
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtCore import Qt, QEvent, QPoint, QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QInputDialog

import serial_tool as st

app = QApplication(sys.argv)

# 离屏环境：屏蔽所有模态对话框，避免阻塞/崩溃
st.QMessageBox.warning = staticmethod(lambda *a, **k: None)
st.QMessageBox.critical = staticmethod(lambda *a, **k: None)
st.QMessageBox.information = staticmethod(lambda *a, **k: None)

# 默认不加载/写入真实配置：每次调用返回全新的空目录（否则本机 serialtool.ini
# 或测试期间写入的 ini 会污染后续用例）
st.MainWindow._ini_path = lambda self: os.path.join(
    tempfile.mkdtemp(prefix="st_test_"), "serialtool.ini")

RESULT = {"pass": 0, "fail": 0, "msgs": []}


def check(name, cond, detail=""):
    if cond:
        RESULT["pass"] += 1
        RESULT["msgs"].append(f"  [PASS] {name}")
    else:
        RESULT["fail"] += 1
        RESULT["msgs"].append(f"  [FAIL] {name}  {detail}")


# ----------------------------------------------------------------------
# 纯函数
# ----------------------------------------------------------------------
def test_crc():
    check("crc16_modbus('123456789')=0x4B37",
          st.crc16_modbus(b"123456789") == 0x4B37,
          f"got {hex(st.crc16_modbus(b'123456789'))}")


def test_build_payload():
    w = st.MainWindow()
    check("payload 文本 GBK", w._build_payload("abc", False) == b"abc")
    check("payload HEX", w._build_payload("0A 0B ff", True) == b"\x0a\x0b\xff")
    check("payload HEX 非法偶数", w._build_payload("0A 0B 0C 0", True) is None)
    check("payload HEX 非法字符", w._build_payload("0A XZ", True) is None)
    check("payload HEX 空", w._build_payload("   ", True) == b"")
    w.close()


def test_apply_check():
    w = st.MainWindow()
    payload = b"0123456789"
    w.cb_check.setCurrentIndex(1)   # CRC16
    w.spin_check_start.setValue(1)
    w.cb_check_end.setCurrentIndex(0)  # 末尾
    out = w._apply_check(payload)
    crc = st.crc16_modbus(payload)
    check("CRC16 全范围", out == payload + bytes((crc & 0xFF, crc >> 8)),
          f"got {out.hex()}")

    w.cb_check.setCurrentIndex(2)   # ADD
    out = w._apply_check(payload)
    check("ADD 累加和", out == payload + bytes((sum(payload) & 0xFF,)))

    w.cb_check.setCurrentIndex(3)   # XOR
    x = 0
    for b in payload:
        x ^= b
    out = w._apply_check(payload)
    check("XOR 异或", out == payload + bytes((x,)))

    # 第2字节至末尾-1
    w.cb_check.setCurrentIndex(1)
    w.spin_check_start.setValue(2)
    w.cb_check_end.setCurrentIndex(1)  # 末尾-1
    out = w._apply_check(payload)
    rng = payload[1:9]
    crc = st.crc16_modbus(rng)
    check("CRC16 第2字节至末尾-1", out == payload + bytes((crc & 0xFF, crc >> 8)),
          f"got {out.hex()}")
    w.close()


def test_newline():
    """主发送：勾选“加回车换行”才追加 \\r\\n"""
    w = st.MainWindow()
    w.cb_newline.setChecked(True)
    w.spin_auto.setValue(1000)

    class FakeSer:
        is_open = True
        written = []

        def write(self, data):
            self.written.append(data)
            return len(data)

    w.ser = FakeSer()
    ok = w._send_payload(False, "abc", add_newline=True)
    check("主发送 newline=True + 勾选 → 追加\\r\\n",
          ok and w.ser.written[-1] == b"abc\r\n", f"got {w.ser.written}")
    w.cb_newline.setChecked(False)
    w._send_payload(False, "abc", add_newline=True)
    check("主发送 newline=True + 未勾选 → 不追加",
          w.ser.written[-1] == b"abc", f"got {w.ser.written}")
    w._send_payload(False, "abc", add_newline=False)
    check("主发送 newline=False → 不追加", w.ser.written[-1] == b"abc")
    w.ser = None
    w.close()


def test_multi_newline():
    """多字符串：应跟随主界面“加回车换行”"""
    w = st.MainWindow()
    w.cb_newline.setChecked(True)

    class FakeSer:
        is_open = True
        written = []

        def write(self, data):
            self.written.append(data)
            return len(data)

    w.ser = FakeSer()
    panel = w.multi_panel
    panel.rows[0]["edit"].setText("reboot wiffo")
    ok = panel.send_row(0)
    check("多字符串发送追加\\r\\n",
          ok and w.ser.written[-1] == b"reboot wiffo\r\n",
          f"got {w.ser.written}")
    w.cb_newline.setChecked(False)
    w.ser.written.clear()
    panel.send_row(0)
    check("多字符串未勾选换行不追加",
          w.ser.written[-1] == b"reboot wiffo", f"got {w.ser.written}")
    w.ser = None
    w.close()


def test_comment():
    """注释编辑：双击字符串框/注释按钮编辑，单击按钮延迟发送（双击不发送）"""
    w = st.MainWindow()
    panel = w.multi_panel
    w.cb_newline.setChecked(True)

    class FakeSer:
        is_open = True
        written = []

        def write(self, data):
            self.written.append(data)
            return len(data)

    w.ser = FakeSer()

    fake = lambda *a, **k: ("重启设备", True)
    orig = QInputDialog.getText
    QInputDialog.getText = staticmethod(fake)
    edit = panel.rows[0]["edit"]
    QTest.mouseDClick(edit, Qt.LeftButton)
    app.processEvents()
    check("双击字符串框可加注释", panel.rows[0]["btn"].text() == "重启设备",
          f"got {panel.rows[0]['btn'].text()!r}")

    panel.rows[1]["edit"].setText("second")
    QTest.mouseDClick(panel.rows[1]["btn"], Qt.LeftButton)
    app.processEvents()
    check("双击注释按钮可加注释", panel.rows[1]["btn"].text() == "重启设备",
          f"got {panel.rows[1]['btn'].text()!r}")
    check("双击注释按钮不发送", w.ser.written == [], f"{w.ser.written}")

    # 单击注释按钮 → 延迟发送
    QTest.mouseClick(panel.rows[1]["btn"], Qt.LeftButton)
    check("单击注释按钮挂起发送", panel._pending_row == 1)
    panel._fire_pending_send()
    check("单击注释按钮完成发送", w.ser.written[-1] == b"second\r\n",
          f"{w.ser.written}")

    # 慢速双击（两击间隔 300ms > 旧 250ms 窗口）→ 仍只编辑注释、不发送
    from PyQt5.QtGui import QMouseEvent
    from PyQt5.QtCore import QPoint
    panel.rows[2]["edit"].setText("slow")
    QTest.mousePress(panel.rows[2]["btn"], Qt.LeftButton)
    check("慢速双击-按下后挂起", panel._pending_row == 2)
    check("慢速双击-间隔内未发送", w.ser.written[-1] == b"second\r\n",
          f"{w.ser.written}")
    QTest.qWait(300)                       # 模拟两击间隔
    dbl = QMouseEvent(QEvent.MouseButtonDblClick, QPoint(5, 5),
                      Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(panel.rows[2]["btn"], dbl)
    check("慢速双击不发送", w.ser.written[-1] == b"second\r\n",
          f"{w.ser.written}")
    check("慢速双击仍打开编辑器", panel.rows[2]["btn"].text() == "重启设备",
          f"got {panel.rows[2]['btn'].text()!r}")

    QInputDialog.getText = staticmethod(lambda *a, **k: ("", True))
    QTest.mouseDClick(panel.rows[0]["edit"], Qt.LeftButton)
    app.processEvents()
    check("清空注释恢复'N无注释'", panel.rows[0]["btn"].text() == "1无注释",
          f"got {panel.rows[0]['btn'].text()!r}")
    QInputDialog.getText = orig
    w.ser = None
    w.close()


def test_export_import():
    w = st.MainWindow()
    panel = w.multi_panel
    panel.rows[0]["edit"].setText("reboot wiffo")
    panel.rows[0]["hex"].setChecked(True)
    panel.rows[0]["order"].setValue(3)
    panel.rows[0]["delay"].setValue(2000)
    panel.set_comment(0, "重启,带逗号")
    panel.rows[1]["edit"].setText("plain")
    panel.rows[1]["order"].setValue(1)

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "strings.ini")
        # 替代文件对话框：直接写入/读取
        from unittest import mock
        st.QFileDialog.getSaveFileName = mock.MagicMock(return_value=(path, ""))
        panel.export_ini()
        check("导出文件已生成", os.path.exists(path))
        # 导入
        st.QFileDialog.getOpenFileName = mock.MagicMock(return_value=(path, ""))
        # 先清空
        for r in panel.rows:
            r["edit"].setText("")
            r["btn"].setText(f"{panel.rows.index(r)+1}无注释")
            r["order"].setValue(0)
            r["comment"] = ""
        panel.import_ini()
        check("导入-内容", panel.rows[0]["edit"].text() == "reboot wiffo")
        check("导入-HEX标记", panel.rows[0]["hex"].isChecked())
        check("导入-顺序", panel.rows[0]["order"].value() == 3)
        check("导入-延时", panel.rows[0]["delay"].value() == 2000)
        check("导入-注释(含逗号)",
              panel.rows[0]["btn"].text() == "重启,带逗号",
              f"got {panel.rows[0]['btn'].text()!r}")
        check("导入-第2条", panel.rows[1]["edit"].text() == "plain")
    w.close()


def test_settings_roundtrip():
    w = st.MainWindow()
    w.cb_hex_send.setChecked(True)
    w.cb_hex_show.setChecked(True)
    w.cb_newline.setChecked(False)
    w.cb_check.setCurrentIndex(2)
    w.spin_timeout.setValue(37)
    w.multi_panel.rows[0]["edit"].setText("hello")
    w.multi_panel.set_comment(0, "打招呼")
    w.multi_panel.rows[0]["order"].setValue(5)
    w.multi_panel.rows[2]["edit"].setText("third")
    w.btn_extend.setChecked(True)

    with tempfile.TemporaryDirectory() as td:
        ini = os.path.join(td, "serialtool.ini")
        st.MainWindow._ini_path = lambda self: ini   # 构造 w2 前先指向临时文件
        w._save_settings()
        w2 = st.MainWindow()
        w2._load_settings()
        check("设置-HEX发送", w2.cb_hex_send.isChecked())
        check("设置-HEX显示", w2.cb_hex_show.isChecked())
        check("设置-回车换行", not w2.cb_newline.isChecked())
        check("设置-校验方式", w2.cb_check.currentIndex() == 2)
        check("设置-超时", w2.spin_timeout.value() == 37)
        check("设置-多字符串内容", w2.multi_panel.rows[0]["edit"].text() == "hello")
        check("设置-多字符串注释", w2.multi_panel.rows[0]["btn"].text() == "打招呼")
        check("设置-多字符串顺序", w2.multi_panel.rows[0]["order"].value() == 5)
        check("设置-扩展状态", w2.btn_extend.isChecked())
        # 恢复默认“不加载配置”
        st.MainWindow._ini_path = lambda self: os.path.join(
            tempfile.mkdtemp(prefix="st_test_"), "serialtool.ini")
        w2.close()
    w.close()


def test_extend_no_resize():
    """扩展/隐藏不应改变窗口大小"""
    w = st.MainWindow()
    w.resize(792, 600)
    w.show()
    app.processEvents()
    w0 = (w.width(), w.height())
    w.btn_extend.setChecked(True)
    app.processEvents()
    QTimer.singleShot(0, app.quit)
    app.exec_()
    w1 = (w.width(), w.height())
    check("点扩展窗口尺寸不变", w0 == w1, f"{w0} -> {w1}")
    check("扩展后面板可见", w.multi_panel.isVisible())
    w.btn_extend.setChecked(False)
    app.processEvents()
    QTimer.singleShot(0, app.quit)
    app.exec_()
    w2 = (w.width(), w.height())
    check("点隐藏窗口尺寸不变", w0 == w2, f"{w0} -> {w2}")
    check("隐藏后面板不可见", not w.multi_panel.isVisible())
    sizes = w.splitter.sizes()
    check("隐藏后接收区占满", sizes[1] == 0, f"sizes={sizes}")
    w.close()


def test_grid_spacing():
    w = st.MainWindow()
    panel = w.multi_panel
    container = panel.findChild(object).__class__  # no-op
    from PyQt5.QtWidgets import QGridLayout
    grids = panel.findChildren(QGridLayout)
    check("网格间距 >= 4", any(g.horizontalSpacing() >= 4 for g in grids),
          f"{[g.horizontalSpacing() for g in grids]}")
    w.close()


def test_cycle():
    w = st.MainWindow()
    panel = w.multi_panel

    class FakeSer:
        is_open = True
        written = []

        def write(self, data):
            self.written.append(data)
            return len(data)

    w.ser = FakeSer()
    panel.rows[0]["edit"].setText("A")
    panel.rows[0]["order"].setValue(1)
    panel.rows[0]["delay"].setValue(50)
    panel.rows[1]["edit"].setText("")      # 空条
    panel.rows[1]["order"].setValue(2)
    panel.rows[1]["delay"].setValue(50)
    panel.rows[2]["edit"].setText("B")
    panel.rows[2]["order"].setValue(3)
    panel.rows[2]["delay"].setValue(50)
    panel.cb_cycle.setChecked(True)          # 触发 toggled -> _on_cycle_toggled
    check("循环-发送A", w.ser.written[-1] == b"A\r\n", f"{w.ser.written}")
    panel.cycle_timer.stop()                 # 手动模拟空条延时到点
    panel._cycle_step()
    check("循环-空条跳过继续", panel.cb_cycle.isChecked())
    panel.cycle_timer.stop()                 # 再模拟下一轮延时到点
    panel._cycle_step()
    check("循环-空条后发送B", w.ser.written[-1] == b"B\r\n", f"{w.ser.written}")
    # 无串口时停止
    w.ser = None
    panel._cycle_step()
    check("循环-串口关闭自动停止", not panel.cb_cycle.isChecked())
    w.close()


def test_format():
    w = st.MainWindow()
    w.cb_hex_show.setChecked(True)
    check("HEX格式化", w._format_rx(b"\x01\x02") == "01 02 ")
    w.cb_hex_show.setChecked(False)
    text = "a\x1b[31mred\x1b[0m uart:~$ ok"
    check("ANSI过滤剥离", w._format_rx(text.encode("gbk")) == "ared ok",
          repr(w._format_rx(text.encode("gbk"))))
    check("逐行前缀", st.MainWindow._prefix_lines("a\nb\n", ">>") == ">>a\n>>b\n")
    w.close()


def test_refresh_ports():
    w = st.MainWindow()
    w.refresh_ports()
    check("刷新串口无异常", w.cb_port.count() >= 1)
    w.close()


def main():
    for fn in [
        test_crc, test_build_payload, test_apply_check, test_newline,
        test_multi_newline, test_comment, test_export_import,
        test_settings_roundtrip, test_extend_no_resize, test_grid_spacing,
        test_cycle, test_format, test_refresh_ports,
    ]:
        try:
            fn()
        except Exception as exc:
            RESULT["fail"] += 1
            import traceback
            RESULT["msgs"].append(f"  [ERROR] {fn.__name__}: {exc}\n{traceback.format_exc()}")
    print("\n".join(RESULT["msgs"]))
    print(f"\n结果: {RESULT['pass']} 通过, {RESULT['fail']} 失败")
    return 1 if RESULT["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
