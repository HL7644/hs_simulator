from ..utils import *


##
# Minions


class ICC_034:
    """Arrogant Crusader"""

    deathrattle = CurrentPlayer(OPPONENT) & Summon(CONTROLLER, "ICC_900t")


class ICC_245:
    """Blackguard"""

    events = Heal(FRIENDLY_HERO).on(Hit(RANDOM_ENEMY_MINION, Heal.AMOUNT))


class ICC_801:
    """Howling Commander"""

    play = ForceDraw(RANDOM(FRIENDLY_DECK + DIVINE_SHIELD))


class ICC_858:
    """Bolvar, Fireblood"""

    events = LosesDivineShield(FRIENDLY_MINIONS).after(Buff(SELF, "ICC_858e"))


ICC_858e = buff(atk=2)


##
# Spells


class ICC_039:
    """Dark Conviction"""

    requirements = {
        PlayReq.REQ_MINION_TARGET: 0,
        PlayReq.REQ_TARGET_TO_PLAY: 0,
    }
    play = Buff(TARGET, "ICC_039e")


class ICC_039e:
    atk = SET(3)
    max_health = SET(3)


class ICC_244:
    """Desperate Stand"""

    requirements = {
        PlayReq.REQ_TARGET_TO_PLAY: 0,
        PlayReq.REQ_MINION_TARGET: 0,
    }
    play = Buff(TARGET, "ICC_244e")


class ICC_244e:
    tags = {GameTag.DEATHRATTLE: True}
    deathrattle = Summon(CONTROLLER, Copy(OWNER)).then(SetCurrentHealth(Summon.CARD, 1))


##
# Weapons


class ICC_071:
    """Light's Sorrow"""

    events = LosesDivineShield(FRIENDLY_MINIONS).after(Buff(SELF, "ICC_071e"))


ICC_071e = buff(atk=1)


##
# Heros


class ICC_829:
    """Uther of the Ebon Blade"""

    play = Summon(CONTROLLER, "ICC_829t")


class ICC_829p:
    requirements = {
        PlayReq.REQ_NUM_MINION_SLOTS: 1,
    }
    entourage = ["ICC_829t2", "ICC_829t3", "ICC_829t4", "ICC_829t5"]
    activate = Summon(CONTROLLER, RandomEntourage(exclude=FRIENDLY_MINIONS))
    update = FindAll(
        FRIENDLY_MINIONS + ID("ICC_829t2"),
        FRIENDLY_MINIONS + ID("ICC_829t3"),
        FRIENDLY_MINIONS + ID("ICC_829t4"),
        FRIENDLY_MINIONS + ID("ICC_829t5"),
    ) & Destroy(ENEMY_HERO)
