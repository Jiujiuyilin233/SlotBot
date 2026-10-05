# 识别测试素材

`credit_*.png` 是裁剪的数字样本。可选的 `seven_screen_*.png` 完整游戏画面夹具不在公开包内，用于固定坐标的 Credit / ChanceMeter 检测、选区完整性和视角漂移回归。没有这些夹具时，相关测试标记为跳过。

完整画面夹具已被 Git 忽略，不应公开上传。项目没有分发个人 `config.json` 或运行日志。

`real_bench.py` 评估样本读取，`bank_bench.py` 使用留一法检查模板错读，`digit_bench.py` 生成合成图供诊断参考。主回归入口为仓库根目录 `regression.py`。
