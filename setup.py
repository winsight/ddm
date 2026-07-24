"""DDM — EDA Data Delivery Manager for PV/PI flow management."""
import re
from pathlib import Path
from setuptools import setup, find_packages


def _get_version() -> str:
    """Read version from ddm/version.py (single source of truth)."""
    vf = Path(__file__).parent / "ddm" / "version.py"
    text = vf.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if m:
        return m.group(1)
    raise RuntimeError("version not found in ddm/version.py")


setup(
    name="ddm",
    version=_get_version(),
    description="芯片模块数据交付管理系统 - EDA PV/PI Data Delivery Manager",
    author="DDM Team",
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        "click==8.1.3",
        "rich==12.6.0",
        "pydantic==1.10.2",
        "PyYAML==6.0",
        "loguru==0.6.0",
        "psutil==5.9.4",
        "blake3==0.3.2",
        "prettytable==3.6.0",
    ],
    entry_points={
        "console_scripts": [
            "ddm=ddm.cli:_cli_entry",
        ],
    },
    python_requires=">=3.8",
)
