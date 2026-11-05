新增脱粉 RUNS 点位包

完整解压后运行 AddRunsPoints.cmd；可先运行 AddRunsPoints.cmd --dry-run。
只向主机现有 points 数组追加以下两点，备份配置，不替换/删除旧 if_prod：
  ns=2;s=TIA_3_3.TIA_3_3.473/RUNS  tag_881030838dd4d05ebcda
  ns=2;s=TIA_3_3.TIA_3_3.474/RUNS  tag_04a29ab776cb07869148

2026-11-02 一次只读 OPC 证据：两点 DataType i=1(ns0)、VariantType Boolean，
原值 bool False、StatusCode=0 Good。False 是有效布尔值，不要求其变 True。
设备真实运行含义由明日现场对照确认；本包不做位图/运行含义推断，不切模型绑定。

保留所有旧点字段、现有账户/安全配置和5秒采集、60秒普通历史、30秒液位策略。
仅停止本安装collector、自然等待旧lease、恢复collector；Web/8000不停止。
成功需新owner/两次提交推进和两个RUNS、两个旧if_prod的新鲜good布尔样本。
失败还原精确旧配置并恢复旧采集；DB与spool保留，无需7GB数据库备份。
遇到外部配置改动时不覆盖未知内容、不盲目启动，请保留窗口联系维护人员。
重复运行不追加重复点；已有点也必须只读验证当前collector及4点健康才报成功。

本包只含配置增量工具，不包含全量collector.json、数据库、密码、模型或DS/研究。
新增字段名称为“473运行反馈（RUNS）”“474运行反馈（RUNS）”。
