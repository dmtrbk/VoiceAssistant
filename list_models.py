import os
from dotenv import load_dotenv
from groq import Groq

# Загружаем переменные из .env
load_dotenv()

api_key = os.getenv("GROQ_API_KEY", "").strip('"\'')

if not api_key:
    print("❌ Ошибка: API-ключ не найден в файле .env.")
    exit(1)

try:
    client = Groq(api_key=api_key)
    # Запрашиваем актуальный список моделей с сервера
    models_list = client.models.list()
    
    print("=== Доступные модели для вашей учетной записи ===")
    for model in models_list.data:
        print(f"- {model.id}")
        
except Exception as e:
    print(f"❌ Не удалось получить список моделей: {e}")
