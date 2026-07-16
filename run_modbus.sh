#!/bin/bash
cd "$(dirname "$0")"
echo "激活虚拟环境..."
source ateq_env/bin/activate
echo "运行程序..."
python3 ateq_modbus.py
echo "完成"
