# 一键清理

一个本地运行的 Windows 桌面文件整理工具。MVP 仅扫描用户选择的来源目录，按年、月和类别预览归档；不联网、不读取文档内容，也不会自动删除文件。

## 开发环境

- Windows 10/11 x64
- Python 3.15
- PySide6 6.12
- SQLite（Python 标准库）

## 启动

```powershell
py -3.15 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m file_organizer
```

本地设置、索引和数据库备份保存在 `%LOCALAPPDATA%\YiJianQingLi`。Git 中只保存源代码和项目说明。

## 安全约定

- 首次启动时选择来源目录和归档目录；每次归档都先预览并由用户确认。
- 不扫描整块磁盘；不进入隐藏/系统目录、快捷方式、符号链接或目录联接点。
- 重名文件使用编号后缀，不覆盖已有文件。
- 跨卷复制完成并通过校验后才移除来源；失败时保留来源文件。
- MVP 不执行删除。撤销只处理最近一批，并跳过冲突项。
