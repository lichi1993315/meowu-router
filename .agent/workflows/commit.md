---
description: commit并更新AGENTS.md文件
---

调用git diff一次性查看我的修改，然后更新AGENTS.md
注意：只更新必要的、重要的内容，在确保和代码一致性的同时，尽量精简
更新完后再生成一个commit摘要, 摘要以markdown格式打印，符合如下格式:
```markdown feat/refactor/...: xxx 

主要变更： 
- xxxxxx 
  - xxx 
  - xxx 
- xxxxxx 
  - xxx 
  - xxx 
```

然后使用git 操作 提交commit并push到远程。