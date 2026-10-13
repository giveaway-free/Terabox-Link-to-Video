"""
bot.py - Universal Bridge to Unified Master TeraStream Bot.
Ensures zero-downtime execution regardless of whether Render executes 'python bot.py'
or 'python terastream_all_in_one.py'.
"""
import sys
import os

if __name__ == "__main__":
    import runpy
    curr_dir = os.path.dirname(os.path.abspath(__file__))
    master_script = os.path.join(curr_dir, "terastream_all_in_one.py")
    runpy.run_path(master_script, run_name="__main__")
