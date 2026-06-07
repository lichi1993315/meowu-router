"""
加密存档工具 - 支持字符串和 JSON 对象的加密保存和读取
"""

import json
import os
from typing import Union, Optional
from cryptography.fernet import Fernet, InvalidToken


class EncryptedSaver:
    """加密存档器，支持字符串和 JSON 对象的加密保存和读取"""

    _KEY_ENV = "PAW_FERNET_KEY"

    def __init__(self, key: bytes | None = None):
        """
        初始化加密器。
        :param key: 32字节的 Fernet 密钥 (bytes 类型)。如果为 None，使用 PAW_FERNET_KEY。
        """
        self.__key = key if key else self._load_key_from_env()
        self.cipher = Fernet(self.__key)

    @classmethod
    def _load_key_from_env(cls) -> bytes:
        key = os.getenv(cls._KEY_ENV, "").strip()
        if not key:
            raise ValueError(f"{cls._KEY_ENV} is not configured")
        return key.encode("utf-8")

    def save(self, data: Union[str, dict, list], filepath: str) -> bool:
        """
        保存数据到加密文件。支持字符串、字典和列表。
        
        :param data: 要保存的数据（str, dict 或 list）
        :param filepath: 保存路径
        :return: 是否保存成功
        """
        try:
            # 根据数据类型转换为字符串
            if isinstance(data, str):
                content_str = data
            else:
                # dict 或 list 转为 JSON 字符串
                content_str = json.dumps(data, ensure_ascii=False)
            
            encrypted_bytes = self.cipher.encrypt(content_str.encode('utf-8'))
            
            # 确保目录存在
            dir_path = os.path.dirname(filepath)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            
            with open(filepath, 'wb') as f:
                f.write(encrypted_bytes)
            
            print(f"存档成功 -> {filepath}")
            return True
            
        except Exception as e:
            print(f"存档失败: {e}")
            return False

    def save_string(self, data: str, filepath: str) -> bool:
        """
        保存字符串到加密文件。
        
        :param data: 要保存的字符串
        :param filepath: 保存路径
        :return: 是否保存成功
        """
        return self.save(data, filepath)

    def save_json(self, data: Union[dict, list], filepath: str) -> bool:
        """
        保存 JSON 对象（dict 或 list）到加密文件。
        
        :param data: 要保存的字典或列表
        :param filepath: 保存路径
        :return: 是否保存成功
        """
        return self.save(data, filepath)

    def load(self, filepath: str) -> Optional[str]:
        """
        读取并解密文件内容，返回字符串。
        
        :param filepath: 文件路径
        :return: 解密后的字符串，失败返回 None
        """
        if not os.path.exists(filepath):
            print(f"未找到存档文件: {filepath}")
            return None
            
        try:
            with open(filepath, 'rb') as f:
                encrypted_content = f.read()
            
            decrypted_bytes = self.cipher.decrypt(encrypted_content)
            return decrypted_bytes.decode('utf-8')
            
        except InvalidToken:
            print("错误：存档文件校验失败（密钥不匹配或内容被非法修改）")
            return None
        except Exception as e:
            print(f"读档失败: {e}")
            return None

    def load_string(self, filepath: str) -> Optional[str]:
        """
        读取并解密文件内容，返回字符串。
        
        :param filepath: 文件路径
        :return: 解密后的字符串，失败返回 None
        """
        return self.load(filepath)

    def load_json(self, filepath: str) -> Optional[Union[dict, list]]:
        """
        读取并解密文件内容，返回 JSON 对象（dict 或 list）。
        
        :param filepath: 文件路径
        :return: 解密后的字典或列表，失败返回 None
        """
        content = self.load(filepath)
        if content is None:
            return None
            
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            print(f"JSON 解析失败: {e}")
            return None

    def encrypt_to_string(self, data: Union[str, dict, list]) -> str:
        """
        将数据加密为 Base64 编码的字符串（不保存到文件）。
        
        :param data: 要加密的数据（str, dict 或 list）
        :return: Base64 编码的加密字符串
        """
        # 根据数据类型转换为字符串
        if isinstance(data, str):
            content_str = data
        else:
            # dict 或 list 转为 JSON 字符串
            content_str = json.dumps(data, ensure_ascii=False)
        
        encrypted_bytes = self.cipher.encrypt(content_str.encode('utf-8'))
        # 返回 Base64 字符串（Fernet 默认输出即为 Base64）
        return encrypted_bytes.decode('utf-8')

    def decrypt_from_string(self, encrypted_str: str) -> Optional[Union[str, dict, list]]:
        """
        从加密字符串解密，自动判断返回字符串或JSON对象。
        
        :param encrypted_str: Base64 编码的加密字符串
        :return: 解密后的数据（str, dict 或 list），失败返回 None
        """
        try:
            decrypted_bytes = self.cipher.decrypt(encrypted_str.encode('utf-8'))
            content = decrypted_bytes.decode('utf-8')
            
            # 尝试解析为JSON
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                # 不是JSON，返回原始字符串
                return content
        except InvalidToken:
            print("错误：解密失败（密钥不匹配或内容被非法修改）")
            return None
        except Exception as e:
            print(f"解密失败: {e}")
            return None

    def decrypt_string_only(self, encrypted_str: str) -> Optional[str]:
        """
        从加密字符串解密，始终返回字符串（不尝试JSON解析）。
        
        :param encrypted_str: Base64 编码的加密字符串
        :return: 解密后的字符串，失败返回 None
        """
        try:
            decrypted_bytes = self.cipher.decrypt(encrypted_str.encode('utf-8'))
            return decrypted_bytes.decode('utf-8')
        except InvalidToken:
            print("错误：解密失败（密钥不匹配或内容被非法修改）")
            return None
        except Exception as e:
            print(f"解密失败: {e}")
            return None

    @staticmethod
    def generate_key() -> bytes:
        """
        生成新的 Fernet 密钥。
        
        :return: 新生成的密钥 (bytes)
        """
        return Fernet.generate_key()


_default_saver: EncryptedSaver | None = None


def _get_default_saver() -> EncryptedSaver:
    global _default_saver
    if _default_saver is None:
        _default_saver = EncryptedSaver()
    return _default_saver


def save_encrypted(data: Union[str, dict, list], filepath: str) -> bool:
    """
    使用默认密钥保存加密数据。
    
    :param data: 要保存的数据（str, dict 或 list）
    :param filepath: 保存路径
    :return: 是否保存成功
    """
    return _get_default_saver().save(data, filepath)


def load_encrypted(filepath: str) -> Optional[str]:
    """
    使用默认密钥读取加密数据（返回字符串）。
    
    :param filepath: 文件路径
    :return: 解密后的字符串，失败返回 None
    """
    return _get_default_saver().load(filepath)


def load_encrypted_json(filepath: str) -> Optional[Union[dict, list]]:
    """
    使用默认密钥读取加密数据（返回 JSON 对象）。
    
    :param filepath: 文件路径
    :return: 解密后的字典或列表，失败返回 None
    """
    return _get_default_saver().load_json(filepath)


def encrypt_to_string(data: Union[str, dict, list]) -> str:
    """
    使用默认密钥将数据加密为字符串。
    
    :param data: 要加密的数据（str, dict 或 list）
    :return: Base64 编码的加密字符串
    """
    return _get_default_saver().encrypt_to_string(data)


def decrypt_from_string(encrypted_str: str) -> Optional[Union[str, dict, list]]:
    """
    使用默认密钥从加密字符串解密。
    
    :param encrypted_str: Base64 编码的加密字符串
    :return: 解密后的数据（str, dict 或 list），失败返回 None
    """
    return _get_default_saver().decrypt_from_string(encrypted_str)


def decrypt_string_only(encrypted_str: str) -> Optional[str]:
    """
    使用默认密钥从加密字符串解密，始终返回字符串。
    
    :param encrypted_str: Base64 编码的加密字符串
    :return: 解密后的字符串，失败返回 None
    """
    return _get_default_saver().decrypt_string_only(encrypted_str)


# --- 测试模块 ---
if __name__ == "__main__":
    # 1. 生成新密钥
    new_key = EncryptedSaver.generate_key()
    print("=" * 50)
    print("【新生成的密钥】(请复制并妥善保管):")
    print(new_key.decode())
    print("=" * 50)
    print("\n正在进行功能测试...")

    # 2. 创建测试加密器
    test_saver = EncryptedSaver(new_key)
    
    # 3. 测试 JSON 数据
    test_json_data = {
        "player": "Hero_01",
        "score": 9999,
        "items": ["光剑", "护盾"],
        "is_new_game": False
    }
    
    json_filename = "test_save_json.dat"
    test_saver.save_json(test_json_data, json_filename)
    loaded_json = test_saver.load_json(json_filename)
    
    if loaded_json == test_json_data:
        print("✓ JSON 测试通过：数据加密并成功还原！")
    else:
        print("✗ JSON 测试失败：还原数据与原数据不符。")
    
    # 4. 测试字符串数据
    test_string_data = "这是一段测试字符串，包含中文和 English 以及数字 12345！"
    
    string_filename = "test_save_string.dat"
    test_saver.save_string(test_string_data, string_filename)
    loaded_string = test_saver.load_string(string_filename)
    
    if loaded_string == test_string_data:
        print("✓ 字符串测试通过：数据加密并成功还原！")
    else:
        print("✗ 字符串测试失败：还原数据与原数据不符。")

    # 5. 清理测试文件
    os.remove(json_filename)
    os.remove(string_filename)
    print("\n测试文件已清理。")
