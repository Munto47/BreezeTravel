"""
阿里云短信验证码发送工具。

当 ALIBABA_CLOUD_ACCESS_KEY_ID 未配置时自动进入 Mock 模式（打印验证码到日志），
方便本地开发和演示，无需真实 SMS 配额。
"""

import logging
from app.config import settings

logger = logging.getLogger(__name__)


async def send_code(phone: str, code: str) -> bool:
    """发送短信验证码，返回是否成功。"""
    if not settings.alibaba_cloud_access_key_id:
        logger.warning(f"[SMS MOCK] 手机号 {phone} 验证码: {code}  (未配置阿里云 Key，Mock 模式)")
        return True

    try:
        from alibabacloud_dysmsapi20170525.client import Client
        from alibabacloud_dysmsapi20170525 import models as sms_models
        from alibabacloud_tea_openapi import models as open_api_models

        config = open_api_models.Config(
            access_key_id=settings.alibaba_cloud_access_key_id,
            access_key_secret=settings.alibaba_cloud_access_key_secret,
            endpoint="dysmsapi.aliyuncs.com",
        )
        client = Client(config)

        request = sms_models.SendSmsRequest(
            phone_numbers=phone,
            sign_name=settings.alibaba_cloud_sms_sign_name,
            template_code=settings.alibaba_cloud_sms_template_code,
            template_param=f'{{"code":"{code}"}}',
        )
        response = client.send_sms(request)
        ok = response.body.code == "OK"
        if not ok:
            logger.error(f"[SMS] 发送失败: {response.body.message}")
        return ok
    except ImportError:
        logger.warning(f"[SMS MOCK] alibabacloud-dysmsapi 未安装，Mock 模式。手机号 {phone} 验证码: {code}")
        return True
    except Exception as e:
        logger.error(f"[SMS] 发送异常: {e}")
        return False
