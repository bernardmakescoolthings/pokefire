"""Run the dashboard and scraper: python -m pokefire."""

from pokefire.viewer import main as viewer_main


def main():
    viewer_main(start_monitor=True)


if __name__ == "__main__":
    main()
