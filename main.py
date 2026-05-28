from dotenv import load_dotenv

load_dotenv()

from bot_handlers import create_application

if __name__ == "__main__":
    app = create_application()
    print("Bot started. Press Ctrl+C to stop.")
    app.run_polling()
