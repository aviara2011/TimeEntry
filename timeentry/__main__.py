"""Run with:  python -m timeentry   (then open http://127.0.0.1:8000)"""
import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="TimeEntry local web app")
    parser.add_argument("--host", default="127.0.0.1", help="Keep 127.0.0.1 so only this computer can reach it")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run("timeentry.web.app:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
