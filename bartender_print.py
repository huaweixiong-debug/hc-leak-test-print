#!/usr/bin/env python3
"""
BarTender 标签打印模块
通过调用 BarTender 命令行工具打印标签
"""

import subprocess
import os
import logging
from typing import Optional, Dict, Any
from pathlib import Path

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bartender_print.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('BarTenderPrint')


class BarTenderPrinter:
    """BarTender 标签打印机类"""
    
    # BarTender 可执行文件路径
    BARTENDER_EXE = r"C:\Program Files\Seagull\BarTender Suite\bartend.exe"
    FIXED_DATA_DIR = Path(r"D:\data")
    FIXED_TEMPLATE = Path(r"D:\data\label.btw")
    
    def __init__(self, template_folder: Optional[str] = None):
        """
        初始化 BarTender 打印机
        
        Args:
            template_folder: 标签模板文件夹路径，默认为当前目录下的 templates 文件夹
        """
        if template_folder is None:
            # 默认模板文件夹
            self.template_folder = Path(__file__).parent / "templates"
        else:
            self.template_folder = Path(template_folder)
        
        logger.info(f"初始化 BarTender 打印机，模板文件夹: {self.template_folder}")
    
    def _get_template_path(self, template_name: str) -> str:
        """
        获取模板文件的完整路径
        
        Args:
            template_name: 模板名称（不含扩展名或含扩展名）
            
        Returns:
            str: 模板文件的完整路径
        """
        # 如果模板名称已包含扩展名
        if template_name.endswith('.btw') or template_name.endswith('.BTW'):
            template_path = self.template_folder / template_name
        else:
            # 自动添加 .btw 扩展名
            template_path = self.template_folder / f"{template_name}.btw"
        
        return str(template_path)
    
    def print_label(self, 
                    template_name: str, 
                    copies: int = 1,
                    variables: Optional[Dict[str, str]] = None,
                    printer_name: Optional[str] = None,
                    wait: bool = True) -> bool:
        """
        打印标签
        
        Args:
            template_name: 模板名称（如 "product_label" 或 "product_label.btw"）
            copies: 打印份数
            variables: 模板变量字典，如 {"SerialNumber": "SN001", "Date": "2026-03-22"}
            printer_name: 指定打印机名称（可选）
            wait: 是否等待打印完成
            
        Returns:
            bool: 打印是否成功
        """
        template_path = self._get_template_path(template_name)
        
        # 检查模板文件是否存在
        if not os.path.exists(template_path):
            logger.error(f"❌ 模板文件不存在: {template_path}")
            return False
        
        # 构建命令行参数
        cmd = [self.BARTENDER_EXE]
        
        # 模板文件路径
        cmd.extend(["/F", template_path])
        
        # 打印份数
        if copies > 1:
            cmd.extend(["/C", str(copies)])
        
        # 指定打印机
        if printer_name:
            cmd.extend(["/P", printer_name])
        
        # 设置变量
        if variables:
            for var_name, var_value in variables.items():
                cmd.extend(["/D", f"{var_name}={var_value}"])
        
        # 打印后关闭 BarTender
        cmd.append("/X")
        
        # 等待打印完成
        if wait:
            cmd.append("/W")
        
        logger.info(f"📄 准备打印标签: {template_name}")
        logger.info(f"   模板路径: {template_path}")
        logger.info(f"   打印份数: {copies}")
        if variables:
            logger.info(f"   变量: {variables}")
        
        try:
            # 执行打印命令
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore'
            )
            
            if result.returncode == 0:
                logger.info(f"✅ 标签打印成功: {template_name}")
                if result.stdout:
                    logger.debug(f"输出: {result.stdout}")
                return True
            else:
                logger.error(f"❌ 标签打印失败，返回码: {result.returncode}")
                if result.stderr:
                    logger.error(f"错误信息: {result.stderr}")
                return False
                
        except Exception as e:
            logger.error(f"❌ 打印异常: {str(e)}")
            return False
    
    def print_with_data(self,
                       template_name: str,
                       serial_number: str,
                       product_model: str,
                       qr_code: Optional[str] = None,
                       date: Optional[str] = None,
                       copies: int = 1) -> bool:
        """
        使用标准数据格式打印标签
        
        Args:
            template_name: 模板名称
            serial_number: 序列号
            product_model: 产品型号
            qr_code: 二维码内容（可选，默认为序列号）
            date: 日期（可选，默认为当前日期）
            copies: 打印份数
            
        Returns:
            bool: 打印是否成功
        """
        from datetime import datetime
        
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        
        if qr_code is None:
            qr_code = serial_number
        
        variables = {
            "SerialNumber": serial_number,
            "ProductModel": product_model,
            "QRCode": qr_code,
            "Date": date
        }
        
        return self.print_label(template_name, copies=copies, variables=variables)

    def print_fixed_label_files(
        self,
        product_model: str,
        supplier_code: str,
        sequence_code: str,
        template_path: str | None = None,
        wait: bool = True,
    ) -> bool:
        """Write D:\\data files and run BarTender with the selected .btw template."""
        self.FIXED_DATA_DIR.mkdir(parents=True, exist_ok=True)

        sequence_code = str(sequence_code or "").strip()
        if sequence_code.isdigit():
            sequence_code = f"{int(sequence_code):05d}"

        files = {
            "产品型号.txt": product_model or "",
            "供应商代码.txt": supplier_code or "",
            "序号.txt": sequence_code,
        }
        encoding = os.environ.get("LABEL_DATA_ENCODING", "gbk")
        for filename, value in files.items():
            (self.FIXED_DATA_DIR / filename).write_text(
                str(value),
                encoding=encoding,
                errors="replace",
            )

        template_text = str(template_path or "").strip()
        if template_text:
            selected_template = Path(template_text)
            if not selected_template.is_absolute():
                if selected_template.suffix.lower() == ".btw":
                    selected_template = self.template_folder / selected_template
                else:
                    selected_template = self.template_folder / f"{template_text}.btw"
        else:
            selected_template = self.FIXED_TEMPLATE
        if not selected_template.exists():
            logger.error("BarTender template does not exist: %s", selected_template)
            return False

        cmd = [self.BARTENDER_EXE, str(selected_template), "/P/X"]
        logger.info(
            "Fixed label data: product_model=%s, supplier_code=%s, sequence=%s, template=%s",
            product_model,
            supplier_code,
            sequence_code,
            selected_template,
        )
        logger.info("Running BarTender: %s", " ".join(cmd))

        try:
            if not wait:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True

            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
            if result.returncode == 0:
                logger.info("Fixed BarTender print command completed")
                return True

            logger.error("Fixed BarTender print failed, return code: %s", result.returncode)
            if result.stderr:
                logger.error("BarTender stderr: %s", result.stderr)
            return False
        except Exception as e:
            logger.error("Fixed BarTender print exception: %s", e)
            return False

    def preview_label(self, template_name: str) -> bool:
        """
        预览标签（打开 BarTender 设计器）
        
        Args:
            template_name: 模板名称
            
        Returns:
            bool: 是否成功打开
        """
        template_path = self._get_template_path(template_name)
        
        if not os.path.exists(template_path):
            logger.error(f"❌ 模板文件不存在: {template_path}")
            return False
        
        cmd = [self.BARTENDER_EXE, "/F", template_path]
        
        try:
            subprocess.Popen(cmd)
            logger.info(f"👁️ 已打开标签预览: {template_name}")
            return True
        except Exception as e:
            logger.error(f"❌ 打开预览失败: {str(e)}")
            return False


def test_print():
    """测试打印功能"""
    print("=" * 60)
    print("BarTender 标签打印测试")
    print("=" * 60)
    
    # 创建打印机实例
    printer = BarTenderPrinter()
    
    # 测试打印（使用示例模板）
    template_name = "test_label"
    
    print(f"\n📄 测试打印标签: {template_name}")
    print("-" * 60)
    
    # 方式1: 使用标准数据格式打印
    success = printer.print_with_data(
        template_name=template_name,
        serial_number="20260322-MODEL001-0001",
        product_model="MODEL001",
        qr_code="20260322-MODEL001-0001",
        copies=1
    )
    
    if success:
        print("\n✅ 打印测试成功")
    else:
        print("\n❌ 打印测试失败")
        print("\n请检查:")
        print("  1. BarTender 是否已安装")
        print(f"  2. 路径是否正确: {printer.BARTENDER_EXE}")
        print(f"  3. 模板文件是否存在: {printer._get_template_path(template_name)}")
    
    print("\n" + "=" * 60)


if __name__ == '__main__':
    test_print()
