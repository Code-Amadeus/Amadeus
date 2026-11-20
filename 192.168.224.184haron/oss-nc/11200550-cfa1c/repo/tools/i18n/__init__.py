# tools/i18n/__init__.py

class I18nAuto:
    """简化版的I18nAuto类，仅用于文本处理"""
    def __init__(self, language="Auto"):
        self.language = language
        
    def __call__(self, text):
        """直接返回原文本，不做翻译"""
        return text