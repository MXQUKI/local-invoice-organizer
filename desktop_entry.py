"""Desktop launcher used by packaged builds."""
import sys

from app import main


if __name__ == "__main__":
    if "--open-browser" not in sys.argv:
        sys.argv.append("--open-browser")
    main()
