"""Isolated health probe; the parent owns its absolute deadline and cancellation."""
import json
import sys


def main(arguments=None):
    from .release import direct_health_check
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 3:
        raise ValueError("Health transport requires target, commit and deadline")
    url, sha, seconds = arguments
    timeout = float(seconds)
    if not 0 < timeout <= 60:
        raise ValueError("Invalid health transport deadline")

    def emit(report):
        # 每个端点一有结论就写一行并刷新：父进程在总时限杀掉本进程时，
        # 已完成的端点结论仍留在输出里，只有卡住的端点保持 unknown。
        print(json.dumps(report), flush=True)

    direct_health_check(url, sha, timeout, emit)


if __name__ == "__main__":
    main()
