"""Explicit owner mapping for ambiguous legacy filenames; originals are kept."""
import argparse
import json
from pathlib import Path
from . import campaign_state, user_profile, runtime


def migrate(source, group_id, user_id=None, user_name=""):
    if not group_id or user_id is not None and not user_id:
        raise ValueError("会话与用户 ID 不能为空")
    source = Path(source).resolve(strict=True)
    content = source.read_text(encoding="utf-8")
    if user_id is not None:
        target = Path(user_profile._get_profile_path(group_id, user_id))
        records = [line for line in content.splitlines() if line.startswith("- [")]
        output = user_profile._header(group_id, user_id, user_name) + "\n".join(records) + "\n"
    else:
        target = Path(campaign_state.CAMPAIGNS_DIR) / (campaign_state._safe_component(group_id) + ".json")
        data = campaign_state._validate(json.loads(content), group_id)
        if not isinstance(data, dict) or not isinstance(data.get("campaigns"), dict):
            raise ValueError("来源不是合法的旧版跑团存档")
        if data.get("_group_id", group_id) != group_id:
            raise ValueError("来源存档属于另一个群")
        data["_group_id"] = group_id
        output = json.dumps(data, ensure_ascii=False, indent=2)
    if target.exists():
        raise FileExistsError(f"目标已存在，未覆盖：{target}")
    runtime.write_text(target, output)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="旧数据文件路径，原文件保留")
    parser.add_argument("--group-id", required=True, help="可信的完整会话 ID")
    parser.add_argument("--user-id", help="迁移画像时必须提供；省略表示跑团存档")
    parser.add_argument("--user-name", default="")
    args = parser.parse_args()
    print(migrate(args.source, args.group_id, args.user_id, args.user_name))


if __name__ == "__main__":
    main()
