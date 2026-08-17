# Agent MongoDB 备份恢复手册

## 备份

在具备 `mongodump` 和 `mongosh` 的运维节点执行：

```powershell
.\scripts\backup_agent_store.ps1 `
  -MongoUri "mongodb://127.0.0.1:27017/?replicaSet=rs0" `
  -Database "yjdl_agent" `
  -OutputDirectory "D:\backups\yjdl-agent"
```

归档和 manifest 必须一起上传到加密的异地存储。生产保留 30 天在线备份、90 天离线归档。

## 隔离恢复与校验

只允许恢复到显式命名的隔离数据库，禁止覆盖生产库：

```powershell
mongorestore --uri="mongodb://127.0.0.1:27017/?replicaSet=rs0" `
  --archive="D:\backups\yjdl-agent\yjdl-agent-<backupId>.archive.gz" `
  --gzip --nsFrom="yjdl_agent.*" --nsTo="yjdl_agent_restore.*"

.\.venv\Scripts\python.exe .\scripts\verify_agent_restore.py `
  --uri "mongodb://127.0.0.1:27017/?replicaSet=rs0" `
  --database "yjdl_agent_restore"
```

校验必须满足：事件序列无缺口、成功 Run 恰好有一条 Memory 和一条 `run.completed`、Checkpoint/State 集合可读。恢复演练未通过时不得进入发布流程。

