"""
Gyna — main.py
Entry point. Delegates entirely to GynaSystemOrchestrator.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from main_orchestrator import GynaSystemOrchestrator, _validate_env

if __name__ == "__main__":
    _validate_env()
    GynaSystemOrchestrator().run()
