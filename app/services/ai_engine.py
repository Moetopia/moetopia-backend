import os
import logging
import numpy as np
from PIL import Image
import onnxruntime as ort
from huggingface_hub import hf_hub_download
from app.core.config import settings

logger = logging.getLogger(__name__)


class AIEngineService:
    _instance = None

    def __new__(cls):
        # 实现单例模式，确保只加载一次模型
        if cls._instance is None:
            cls._instance = super(AIEngineService, cls).__new__(cls)
            cls._instance._initialize_engine()
        return cls._instance

    def _initialize_engine(self):
        logger.info("🤖 正在初始化 WD14 AI 引擎并加载权重 (仅执行一次)...")
        os.environ["HF_ENDPOINT"] = settings.HF_ENDPOINT

        try:
            import pandas as pd
            
            # 下载 ONNX 模型
            self.onnx_path = hf_hub_download(repo_id="SmilingWolf/wd-v1-4-convnext-tagger-v2", filename="model.onnx")
            # 下载标签文件
            self.tags_path = hf_hub_download(repo_id="SmilingWolf/wd-v1-4-convnext-tagger-v2", filename="selected_tags.csv")
            
            # 加载标签映射表
            df = pd.read_csv(self.tags_path)
            self.tag_names = df["name"].tolist()

            # 优先使用 GPU 加速
            providers = ['CUDAExecutionProvider', 'CPUExecutionProvider'] if 'CUDAExecutionProvider' in ort.get_available_providers() else ['CPUExecutionProvider']
            self.session = ort.InferenceSession(self.onnx_path, providers=providers)

            self.input_name = self.session.get_inputs()[0].name
            self.output_name = self.session.get_outputs()[0].name
            logger.info(f"✅ AI 引擎就绪！加速提供者: {providers[0]} | 成功加载 {len(self.tag_names)} 个分类标签。")
        except Exception as e:
            logger.error(f"❌ AI 引擎加载失败: {e}")
            raise e

    def preprocess_image(self, img_path: str) -> np.ndarray:
        """图片预处理标准化"""
        img = Image.open(img_path).convert('RGBA')
        bg = Image.new('RGBA', img.size, (255, 255, 255))
        alpha_comp = Image.alpha_composite(bg, img).convert('RGB')
        w, h = alpha_comp.size
        max_dim = max(w, h)
        pad_img = Image.new('RGB', (max_dim, max_dim), (255, 255, 255))
        pad_img.paste(alpha_comp, ((max_dim - w) // 2, (max_dim - h) // 2))
        pad_img = pad_img.resize((448, 448), Image.Resampling.LANCZOS)
        img_array = np.array(pad_img, dtype=np.float32)[:, :, ::-1]
        return np.expand_dims(img_array, axis=0)

    def extract_vector(self, image_path: str, threshold: float = 0.35) -> tuple[list[float], list[dict]]:
        """
        提取图片的 9083 维语义特征向量，并解析出置信度大于 threshold 的有效标签
        """
        tensor = self.preprocess_image(image_path)
        probs = self.session.run([self.output_name], {self.input_name: tensor})[0][0]
        
        # 解析 Tags
        tags_with_conf = []
        # 前 4 个索引往往是 rating (general, sensitive, questionable, explicit)，我们从 index 4 开始提取标签
        for i, p in enumerate(probs[4:], start=4):
            if p >= threshold:
                tags_with_conf.append({
                    "tag": self.tag_names[i],
                    "confidence": float(p)
                })
                
        return probs.tolist(), tags_with_conf


# 导出全局单例
ai_engine = AIEngineService()