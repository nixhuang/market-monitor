# 自动化 9157b538 —— 推送已提交改动并核验线上生效

## 2026-10-15 21:35 执行（一次性任务，已完成）

- 目的：把本地「灯圈空心 + 统计列居中 + signal_single_open 开关」那笔改动推上线并核验。
- 结果：**成功**。rebase 后提交号 27e7a65；测试 9/9；push 成功；三条流水线全部 success；线上核验 5 项全部命中。

## 经验（下次照做）

- 远端常有流水线自动产物提交（只改 index.html/status.json），与本地改动通常不冲突，rebase 即可；本地提交号会变，报告要用 rebase 后的新号。
- 轮询流水线时，判断条件不能只数 "completed success" 的条数（自动测试和 Pages 会很快 success，把还在跑的市场自检掩盖掉），必须按 name 单独取「市场自检」的 status。
- 线上 index.html 会被「市场自检」的产物提交再次覆盖并触发新一次 Pages 部署，所以核验页面必须等市场自检完成后再抓，且最好一次抓完所有检查项（分两次抓可能拿到不同版本）。
- 统计 `range52-missing` 会命中 CSS 定义里的那一次；计「缺失条数」要用 `<span class="range52-missing"`。
- 核验脚本放系统临时目录（%TEMP%），别放工作目录，避免污染仓库未跟踪文件。
- python 输出中文需 `PYTHONIOENCODING=utf-8`。
