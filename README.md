# SerialTool 项目说明

这是一个 Windows 下的串口调试工具项目。

## 目录说明

- `src/`：源码目录
- `exe/`：打包输出目录
- `build/`：PyInstaller 临时构建目录

## 代码在哪

- 主程序：`src/serial_tool.py`
- 图标资源：`src/icon.ico`
- 打包脚本：`src/build.bat`

## exe 在哪

- 生成后的程序：`exe/SerialTool.exe`

## 怎么打包

直接双击 `src/build.bat`。

脚本会自动：

1. 检查 `pyinstaller`
2. 关闭正在运行的 `SerialTool.exe`
3. 用 PyInstaller 打包
4. 输出到 `exe/SerialTool.exe`

## 备注

- 程序窗口图标和任务栏图标都使用 `src/icon.ico`
- 如果你改了代码，重新跑一次 `src/build.bat` 就行
