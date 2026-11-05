主机源码版本只读核对

将整个 ZIP 解压到程序安装目录之外的新文件夹，双击 DiagnoseProject.cmd。
出现 SOURCE CAPTURE UPLOADED 即已回传，可以关闭窗口。

本工具只读取以下四个安装中的 Python 源文件原字节：
backend/app/telemetry/derived.py
backend/app/hourly_brief/inputs.py
backend/app/services/decision_sources.py
backend/app/decision_hourly/history.py

报告包含文件大小、SHA256、UTF-8 BOM、CRLF/单独LF/单独CR计数，以及压缩后的原字节。
不导入或运行应用源码，不扫描目录，不读取数据库、现场配置、模型、环境变量列表或日志。
不停止/启动服务，不改安装文件，不改点位或参数。
只在本解压文件夹保存 source-capture.json，并通过既有诊断地址回传到笔记本。
若回传失败，请保留该 JSON 文件；系统采集与网页不受影响。
