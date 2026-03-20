from __future__ import annotations

from .server import server

def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

