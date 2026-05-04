import json
import os
from datetime import datetime

PROFILES_DIR = "data/user_profiles"

class UserProfileManager:
    def get_or_create(self, user_id: str, name: str = "") -> dict:
        path = os.path.join(PROFILES_DIR, f"{user_id}.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        profile = {
            "user_id": user_id,
            "name": name,
            "role": "unknown",
            "experience_level": "unknown",
            "preferred_detail": "verbose",
            "interaction_count": 0,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }
        self._save(profile)
        return profile

    def update(self, user_id: str, updates: dict) -> dict:
        profile = self.get_or_create(user_id)
        profile.update(updates)
        profile["updated_at"] = datetime.utcnow().isoformat()
        profile["interaction_count"] += 1
        self._save(profile)
        return profile

    def _save(self, profile: dict):
        os.makedirs(PROFILES_DIR, exist_ok=True)
        path = os.path.join(PROFILES_DIR, f"{profile['user_id']}.json")
        with open(path, "w") as f:
            json.dump(profile, f, indent=2)
