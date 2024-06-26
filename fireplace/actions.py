import random
from collections import OrderedDict

from hearthstone.enums import (
    BlockType,
    CardClass,
    CardType,
    GameTag,
    Mulligan,
    PlayState,
    Race,
    Step,
    Zone,
)

from .dsl import LazyNum, LazyValue, Selector
from .dsl.copy import Copy, RebornCopy
from .dsl.random_picker import RandomMinion
from .dsl.selector import SELF
from .entity import Entity
from .enums import DISCARDED
from .exceptions import InvalidAction
from .logging import log
from .utils import random_class


def _eval_card(source, card):
    """
    Return a Card instance from \a card
    The card argument can be:
    - A Card instance (nothing is done)
    - The string ID of the card (the card is created)
    - A LazyValue (the card is dynamically created)
    - A Selector (take entity lists and returns a sub-list)
    """
    if isinstance(card, LazyValue):
        card = card.evaluate(source)

    if isinstance(card, Action):
        card = card.trigger(source)[0]

    if isinstance(card, Selector):
        card = card.eval(source.game, source)

    if not isinstance(card, list):
        cards = [card]
    else:
        cards = card

    ret = []
    for card in cards:
        if isinstance(card, str):
            ret.append(source.controller.card(card, source))
        else:
            ret.append(card)

    return ret


class EventListener:
    ON = 1
    AFTER = 2

    def __init__(self, trigger, actions, at):
        self.trigger = trigger
        self.actions = actions
        self.at = at
        self.once = False

    def __repr__(self):
        return "<EventListener %r>" % (self.trigger)


class ActionMeta(type):
    def __new__(metacls, name, bases, namespace):
        cls = type.__new__(metacls, name, bases, dict(namespace))
        argslist = []
        for k, v in namespace.items():
            if not isinstance(v, ActionArg):
                continue
            v._setup(len(argslist), k, cls)
            argslist.append(v)
        cls.ARGS = tuple(argslist)
        return cls

    @classmethod
    def __prepare__(metacls, name, bases):
        return OrderedDict()


class ActionArg(LazyValue):
    def _setup(self, index, name, owner):
        self.index = index
        self.name = name
        self.owner = owner

    def __repr__(self):
        return "<%s.%s>" % (self.owner.__name__, self.name)

    def evaluate(self, source):
        # This is used when an event listener triggers and the callback
        # Action has arguments of the type Action.FOO
        # XXX we rely on source.event_args to be set, but it's very racey.
        # If multiple events happen on an entity at once, stuff will go wrong.
        assert source.event_args
        return source.event_args[self.index]


class CardArg(ActionArg):
    # Type hint
    pass


class IntArg(ActionArg, LazyNum):
    def evaluate(self, source):
        ret = super().evaluate(source)
        return self.num(ret)


class Action(metaclass=ActionMeta):
    def __init__(self, *args, **kwargs):
        self._args = args
        self._kwargs = kwargs
        self.callback = ()
        self.times = 1
        self.event_queue = []
        self.choice_callback = []

    def __repr__(self):
        args = ["%s=%r" % (k, v) for k, v in zip(self.ARGS, self._args)]
        return "<Action: %s(%s)>" % (self.__class__.__name__, ", ".join(args))

    def after(self, *actions):
        return EventListener(self, actions, EventListener.AFTER)

    def on(self, *actions):
        return EventListener(self, actions, EventListener.ON)

    def then(self, *args):
        """
        Create a callback containing an action queue, called upon the
        action's trigger with the action's arguments available.
        """
        ret = self.__class__(*self._args, **self._kwargs)
        ret.callback = args
        ret.times = self.times
        return ret

    def _broadcast(self, entity, source, at, *args):
        for event in entity.events:
            if event.at != at:
                continue
            if isinstance(event.trigger, self.__class__) and event.trigger.matches(
                entity, source, args
            ):
                log.info("%r triggers off %r from %r", entity, self, source)
                entity.trigger_event(source, event, args)
                if (
                    entity.type == CardType.SPELL
                    and entity.data.secret
                    and entity.controller.extra_trigger_secret
                ):
                    entity.trigger_event(source, event, args)

    def broadcast(self, source, at, *args):
        source.game.action_start(BlockType.TRIGGER, source, 0, None)

        for entity in source.game.entities:
            self._broadcast(entity, source, at, *args)
        for hand in source.game.hands:
            for entity in hand.entities:
                self._broadcast(entity, source, at, *args)
        for deck in source.game.decks:
            for entity in deck.entities:
                self._broadcast(entity, source, at, *args)

        source.game.action_end(BlockType.TRIGGER, source)

    def queue_broadcast(self, obj, args):
        self.event_queue.append((obj, args))

    def resolve_broadcasts(self):
        for obj, args in self.event_queue:
            obj.broadcast(*args)
        self.event_queue = []

    def get_args(self, source):
        return self._args

    def matches(self, entity, source, args):
        for arg, match in zip(args, self._args):
            if match is None:
                # Allow matching Action(None, None, z) to Action(x, y, z)
                continue
            if arg is None:
                # We got an arg of None and a match not None. Bad.
                return False
            if callable(match):
                res = match(arg)
                if not res:
                    return False
            else:
                # this stuff is stupidslow
                res = match.eval([arg], entity)
                if not res or res[0] is not arg:
                    return False
        if hasattr(self, "source") and self.source:
            res = self.source.eval([source], entity)
            if not res or res[0] is not source:
                return False
        return True

    def trigger_choice_callback(self):
        callbacks = self.choice_callback
        self.choice_callback = []
        for callback in callbacks:
            callback()


class GameAction(Action):
    def trigger(self, source):
        args = self.get_args(source)
        self.do(source, *args)


class Attack(GameAction):
    """
    Make \a ATTACKER attack \a DEFENDER
    """

    ATTACKER = ActionArg()
    DEFENDER = ActionArg()

    def get_args(self, source):
        attackers = _eval_card(source, self._args[0])
        attacker = attackers[0] if attackers else None
        defenders = _eval_card(source, self._args[1])
        defender = defenders[0] if defenders else None
        return attacker, defender

    def do(self, source, attacker, defender):
        log.info("%r attacks %r", attacker, defender)
        if not attacker or not defender:
            return
        attacker.attack_target = defender
        defender.defending = True
        source.game.proposed_attacker = attacker
        source.game.proposed_defender = defender
        source.game.manager.step(Step.MAIN_COMBAT, Step.MAIN_ACTION)
        source.game.refresh_auras()  # XXX Needed for Gorehowl
        source.game.manager.game_action(self, source, attacker, defender)
        self.broadcast(source, EventListener.ON, attacker, defender)

        defender = source.game.proposed_defender
        source.game.proposed_attacker = None
        source.game.proposed_defender = None
        if attacker.should_exit_combat:
            log.info("Attack has been interrupted.")
            attacker.attack_target = None
            defender.defending = False
            return

        assert attacker is not defender, "Why are you hitting yourself %r?" % (attacker)

        # Save the attacker/defender atk values in case they change during the attack
        # (eg. in case of Enrage)
        def_atk = defender.atk
        source.game.queue_actions(attacker, [Hit(defender, attacker.atk)])
        if def_atk:
            source.game.queue_actions(defender, [Hit(attacker, def_atk)])

        self.broadcast(source, EventListener.AFTER, attacker, defender)

        attacker.attack_target = None
        defender.defending = False
        if source == attacker:
            attacker.num_attacks += 1


class BeginTurn(GameAction):
    """
    Make \a player begin the turn
    """

    PLAYER = ActionArg()

    def do(self, source, player):
        source.manager.step(source.next_step, Step.MAIN_READY)
        source.turn += 1
        source.log("%s begins turn %i", player, source.turn)
        source.current_player = player
        source.manager.step(source.next_step, Step.MAIN_START_TRIGGERS)
        source.manager.step(source.next_step, source.next_step)
        source.game.manager.game_action(self, source, player)
        self.broadcast(source, EventListener.ON, player)
        source._begin_turn(player)


class Concede(GameAction):
    """
    Make \a player concede
    """

    PLAYER = ActionArg()

    def do(self, source, player):
        player.playstate = PlayState.CONCEDED
        source.game.manager.game_action(self, source, player)
        source.game.check_for_end_game()


class Disconnect(GameAction):
    """
    Make \a player disconnect
    """

    PLAYER = ActionArg()

    def do(self, source, player):
        player.playstate = PlayState.DISCONNECTED
        source.game.manager.game_action(self, source, player)


class Deaths(GameAction):
    """
    Process all deaths in the PLAY Zone.
    """

    def do(self, source, *args):
        source.game.process_deaths()


class Death(GameAction):
    """
    Move target to the GRAVEYARD Zone.
    """

    ENTITY = ActionArg()

    def _broadcast(self, entity, source, at, *args):
        # https://github.com/jleclanche/fireplace/issues/126
        target = args[0]
        if (not self._trigger) and entity.play_counter > target.play_counter:
            self._trigger = True
            if at == EventListener.ON and target.has_deathrattle:
                source.game.queue_actions(target, [Deathrattle(target)])
            if (
                at == EventListener.AFTER
                and target.type == CardType.MINION
                and target.reborn
            ):
                source.game.queue_actions(
                    target, [Summon(target.controller, RebornCopy(SELF))]
                )
        return super()._broadcast(entity, source, at, *args)

    def do(self, source, cards):
        for card in cards:
            if not card.dead:
                continue
            if card.zone == Zone.PLAY:
                card._dead_position = card.zone_position - 1
            card.zone = Zone.GRAVEYARD
            source.game.check_for_end_game()
            source.game.refresh_auras()
            log.info("Processing Deathrattle for %r", card)
            self._trigger = False
            source.game.manager.game_action(self, source, card)
            self.broadcast(source, EventListener.ON, card)

        for card in cards:
            if not card.dead:
                continue
            self._trigger = False
            self.broadcast(source, EventListener.AFTER, card)


class EndTurn(GameAction):
    """
    End the current turn
    """

    PLAYER = ActionArg()

    def do(self, source, player):
        if player.choice:
            raise InvalidAction(
                "%r cannot end turn with the open choice %r." % (player, player.choice)
            )
        source.game.manager.game_action(self, source, player)
        self.broadcast(source, EventListener.ON, player)
        if player.extra_end_turn_effect:
            self.broadcast(source, EventListener.ON, player)
        source.game._end_turn()


class Joust(GameAction):
    """
    Perform a joust between \a challenger and \a defender.
    Note that this does not evaluate the results of the joust. For that,
    see dsl.evaluators.JoustEvaluator.
    """

    CHALLENGER = ActionArg()
    DEFENDER = ActionArg()

    def get_args(self, source):
        challenger = self._args[0].eval(source.game, source)
        defender = self._args[1].eval(source.game, source)
        return challenger and challenger[0], defender and defender[0]

    def do(self, source, challenger, defender):
        log.info("Jousting %r vs %r", challenger, defender)
        source.game.manager.game_action(self, source, challenger, defender)
        source.game.joust(source, challenger, defender, self.callback)


class MulliganChoice(GameAction):
    PLAYER = ActionArg()

    def __init__(self, *args, callback):
        super().__init__(*args)
        self.callback = callback

    def do(self, source, player):
        player.mulligan_state = Mulligan.INPUT
        player.choice = self
        # NOTE: Ideally, we give The Coin when the Mulligan is over.
        # Unfortunately, that's not compatible with Blizzard's way.
        self.cards = player.hand.exclude(id="GAME_005")
        self.source = source
        self.player = player
        self.min_count = 0
        # but weirdly, the game server includes the coin in the mulligan count
        self.max_count = len(player.hand)
        source.game.manager.game_action(self, source, player)

    #input: cards subject to be replaced.
    def choose(self, *cards):
        for card in cards:
            assert card in self.cards
        self.player.choice = None
        #print("\ninside actions.choose")
        for card in cards:
            #print(card.zone_position, card)
            card._summon_index = 0
            #draw from 0 idx, drawing from -1 idx results in drawing the card that player just shuffled in. -> decks are randomly shuffled - so it's random draw.
            new_card = self.player.deck[0]
            new_card._summon_index = card.zone_position
            #print(new_card.zone_position, new_card._summon_index, new_card)

            #remove the card from hand and add it to the deck, then add the new card to the hand
            card.zone = Zone.DECK
            new_card.zone = Zone.HAND
            #move the shuffling function.
        self.player.shuffle_deck()
        self.player.mulligan_state = Mulligan.DONE

        if self.player.opponent.mulligan_state == Mulligan.DONE:
            self.callback()


class Play(GameAction):
    """
    Make the source player play \a card, on \a target or None.
    Choose play action from \a choose or None.
    """

    PLAYER = ActionArg()
    CARD = CardArg()
    TARGET = ActionArg()
    INDEX = IntArg()
    CHOOSE = ActionArg()

    def _broadcast(self, entity, source, at, *args):
        # Prevent cards from triggering off their own play
        if entity is args[1]:
            return
        return super()._broadcast(entity, source, at, *args)

    def do(self, source, card, target, index, choose):
        player = source
        log.info("%s plays %r (target=%r, index=%r)", player, card, target, index)

        player.pay_cost(card, card.cost)

        card.target = target
        card._summon_index = index

        battlecry_card = choose or card
        # We check whether the battlecry will trigger, before the card.zone changes
        if battlecry_card.battlecry_requires_target() and not target:
            log.info("%r requires a target for its battlecry. Will not trigger.")
            trigger_battlecry = False
        else:
            trigger_battlecry = True

        card.play_left_most = card is card.controller.hand[0]
        card.play_right_most = card is card.controller.hand[-1]

        card.zone = Zone.PLAY

        # Remember cast on friendly characters
        if card.type == CardType.SPELL and target and target.controller == source:
            card.cast_on_friendly_characters = True
            if target.type == CardType.MINION:
                card.cast_on_friendly_minions = True

        source.game.manager.game_action(self, source, card, target, index, choose)
        # NOTE: A Play is not a summon! But it sure looks like one.
        # We need to fake a Summon broadcast.
        summon_action = Summon(player, card)

        if card.type == CardType.SPELL and card.twinspell:
            source.game.queue_actions(card, [Give(player, card.twinspell_copy)])

        if card.type in (CardType.MINION, CardType.WEAPON):
            self.queue_broadcast(
                summon_action, (player, EventListener.ON, player, card)
            )
        self.broadcast(player, EventListener.ON, player, card, target)
        self.resolve_broadcasts()

        # "Can't Play" (aka Counter) means triggers don't happen either
        if not card.cant_play:
            if card.play_outcast and card.get_actions("outcast"):
                source.game.trigger(card, card.get_actions("outcast"), event_args=None)
            elif trigger_battlecry:
                source.game.queue_actions(
                    card, [Battlecry(battlecry_card, card.target)]
                )

            if card.echo:
                source.game.queue_actions(
                    card, [Give(player, Buff(Copy(SELF), "GIL_000"))]
                )

            actions = card.get_actions("magnetic")
            if actions:
                source.game.trigger(card, actions, event_args=None)

            # If the play action transforms the card (eg. Druid of the Claw), we
            # have to broadcast the morph result as minion instead.
            played_card = card.morphed or card
            played_card.play_right_most = card.play_right_most
            if played_card.type in (CardType.MINION, CardType.WEAPON):
                summon_action.broadcast(
                    player, EventListener.AFTER, player, played_card
                )
            self.broadcast(player, EventListener.AFTER, player, played_card, target)

        player.combo = True
        player.last_card_played = card
        if card.type == CardType.MINION:
            player.minions_played_this_turn += 1
            if Race.TOTEM in card.races:
                card.controller.times_totem_summoned_this_game += 1
            if Race.ELEMENTAL in card.races:
                player.elemental_played_this_turn += 1
        player.cards_played_this_turn += 1
        player.cards_played_this_game.append(card)
        card.turn_played = source.game.turn
        card.choose = None


class Activate(GameAction):
    PLAYER = ActionArg()
    CARD = CardArg()
    TARGET = ActionArg()
    CHOOSE = ActionArg()

    def get_args(self, source):
        return (source,) + super().get_args(source)

    def do(self, source, player, heropower, target, choose):
        player.pay_cost(heropower, heropower.cost)
        source.game.manager.game_action(self, source, player, heropower, target, choose)
        self.broadcast(source, EventListener.ON, player, heropower, target, choose)

        card = choose or heropower
        source.game.action_start(BlockType.PLAY, heropower, 0, target)
        source.game.queue_actions(source, [PlayHeroPower(card, target)])
        source.game.action_end(BlockType.PLAY, heropower)

        for entity in player.live_entities:
            if not entity.ignore_scripts:
                actions = entity.get_actions("inspire")
                if actions:
                    source.game.trigger(entity, actions, event_args=None)

        self.broadcast(source, EventListener.AFTER, player, heropower, target, choose)
        heropower.activations_this_turn += 1


class Overload(GameAction):
    PLAYER = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, player, amount):
        if player.cant_overload:
            log.info("%r cannot overload %s", source, player)
            return
        log.info("%r overloads %s for %i", source, player, amount)
        source.game.manager.game_action(self, source, player, amount)
        self.broadcast(source, EventListener.ON, player, amount)
        player.overloaded += amount
        player.overloaded_this_game += amount


class TargetedAction(Action):
    TARGET = ActionArg()

    def __init__(self, *args, **kwargs):
        self.source = kwargs.pop("source", None)
        super().__init__(*args, **kwargs)
        self.trigger_index = 0

    def __repr__(self):
        args = ["%s=%r" % (k, v) for k, v in zip(self.ARGS[1:], self._args[1:])]
        return "<TargetedAction: %s(%s)>" % (self.__class__.__name__, ", ".join(args))

    def __mul__(self, value):
        self.times = value
        return self

    def eval(self, selector, source):
        if isinstance(selector, Entity):
            return [selector]
        else:
            return selector.eval(source.game, source)

    def get_target_args(self, source, target):
        ret = []
        for k, v in zip(self.ARGS[1:], self._args[1:]):
            if isinstance(v, Selector):
                # evaluate Selector arguments
                v = v.eval(source.game, source)
            elif isinstance(v, LazyValue):
                v = v.evaluate(source)
            elif isinstance(v, Action):
                v = v.trigger(source)[0]
            elif isinstance(k, CardArg):
                v = _eval_card(source, v)
            ret.append(v)
        return ret

    def get_targets(self, source, t):
        if isinstance(t, Entity):
            ret = t
        elif isinstance(t, LazyValue):
            ret = t.evaluate(source)
        elif isinstance(t, str):
            ret = source.controller.card(t, source=source)
        elif isinstance(t, Action):
            ret = t.trigger(source)[0]
        else:
            ret = t.eval(source.game, source)
        if not ret:
            return []
        if not hasattr(ret, "__iter__"):
            # Bit of a hack to ensure we always get a list back
            ret = [ret]
        return ret

    def trigger(self, source):
        ret = []

        if self.source is not None and isinstance(self.source, Selector):
            source = self.source.eval(source.game, source)
            assert len(source) == 1
            source = source[0]

        times = self.times
        if isinstance(times, LazyValue):
            times = times.evaluate(source)
        elif isinstance(times, Action):
            times = times.trigger(source)[0]
        elif isinstance(times, Selector):
            times = times.eval(source.game, source)

        for i in range(times):
            ret += self._trigger(i, source)

        self.resolve_broadcasts()

        return ret

    def _trigger(self, i, source):
        if source.controller.choice:
            self.choice_callback.append(lambda: self._trigger(i, source))
            return []
        ret = []
        self.trigger_index = i
        args = self.get_args(source)
        targets = self.get_targets(source, args[0])
        args = args[1:]
        log.info("%r triggering %r targeting %r", source, self, targets)
        for target in targets:
            target_args = self.get_target_args(source, target)
            ret.append(self.do(source, target, *target_args))

            for action in self.callback:
                log.info("%r queues up callback %r", self, action)
                ret += source.game.queue_actions(
                    source, [action], event_args=[target] + target_args
                )
        return ret


class Buff(TargetedAction):
    """
    Buff character targets with Enchantment \a id
    NOTE: Any Card can buff any other Card. The controller of the
    Card that buffs the target becomes the controller of the buff.
    """

    TARGET = ActionArg()
    BUFF = ActionArg()

    def get_target_args(self, source, target):
        buff = self._args[1]
        buff = source.controller.card(buff, source=source)
        buff.source = source
        return [buff]

    def do(self, source, target, buff):
        kwargs = self._kwargs.copy()
        for k, v in kwargs.items():
            if isinstance(v, LazyValue):
                v = v.evaluate(source)
            setattr(buff, k, v)
        buff.apply(target)
        source.game.manager.targeted_action(self, source, target, buff)
        return target


class MultiBuff(TargetedAction):
    TARGET = ActionArg()
    BUFFS = ActionArg()

    def get_target_args(self, source, target):
        buffs = self._args[1]
        buffs = [source.controller.card(buff, source=source) for buff in buffs]
        for buff in buffs:
            buff.source = source
        return [buffs]

    def do(self, source, target, buffs):
        for buff in buffs:
            kwargs = self._kwargs.copy()
            for k, v in kwargs.items():
                if isinstance(v, LazyValue):
                    v = v.evaluate(source)
                setattr(buff, k, v)
            buff.apply(target)
            source.game.manager.targeted_action(self, source, target, buff)
        return target


class StoringBuff(TargetedAction):
    TARGET = ActionArg()
    BUFF = ActionArg()
    CARD = ActionArg()

    def get_target_args(self, source, target):
        buff = self._args[1]
        card = _eval_card(source, self._args[2])[0]
        buff = source.controller.card(buff, source=source)
        buff.source = source
        return [buff, card]

    def do(self, source, target, buff, card):
        log.info("%r store card %r", buff, card)
        buff.store_card = card
        return buff.apply(target)


class Bounce(TargetedAction):
    """
    Bounce minion targets on the field back into the hand.
    """

    def do(self, source, target):
        if len(target.controller.hand) >= target.controller.max_hand_size:
            log.info("%r is bounced to a full hand and gets destroyed", target)
            return source.game.queue_actions(source, [Destroy(target)])
        else:
            log.info("%r is bounced back to %s's hand", target, target.controller)
            target.zone = Zone.HAND
            source.game.manager.targeted_action(self, source, target)


class Choice(TargetedAction):
    CARDS = ActionArg()
    CARD = ActionArg()

    def get_target_args(self, source, target):
        cards = self._args[1]
        if isinstance(cards, Selector):
            cards = cards.eval(source.game, source)
        elif isinstance(cards, LazyValue):
            cards = cards.evaluate(source)
        elif isinstance(cards, list):
            eval_cards = []
            for card in cards:
                if isinstance(card, LazyValue):
                    eval_cards.append(card.evaluate(source)[0])
                elif isinstance(card, str):
                    eval_cards.append(source.controller.card(card, source))
                else:
                    eval_cards.append(card)
            cards = eval_cards

        return [cards]

    def do(self, source, player, cards):
        if len(cards) == 0:
            return
        log.info("%r choice from %r", player, cards)
        player.choice = self
        self._callback = self.callback
        self.callback = []
        self.source = source
        self.player = player
        self.cards = cards
        self.min_count = 1
        self.max_count = 1
        source.game.manager.targeted_action(self, source, player, cards)

    def choose(self, card):
        if card not in self.cards:
            raise InvalidAction(
                "%r is not a valid choice (one of %r)" % (card, self.cards)
            )
        self.player.choice = None
        for action in self._callback:
            self.source.game.trigger(self.source, [action], [self.cards, card])
        self.callback = self._callback
        self.trigger_choice_callback()


class GenericChoice(Choice):
    def choose(self, card):
        super().choose(card)
        for _card in self.cards:
            if _card is card:
                if card.type == CardType.HERO_POWER:
                    _card.zone = Zone.PLAY
                elif len(self.player.hand) < self.player.max_hand_size:
                    _card.zone = Zone.HAND
                else:
                    _card.discard()
            else:
                _card.discard()


class CopyDeathrattleBuff(TargetedAction):
    """
    Copy the deathrattles from a card onto the target
    """

    TARGET = ActionArg()
    Buff = ActionArg()

    def get_target_args(self, source, target):
        buff = self._args[1]
        buff = source.controller.card(buff, source=source)
        buff.tags[GameTag.DEATHRATTLE] = True
        buff.source = source
        return [buff]

    def create_buff(self, source):
        buff = self._args[1]
        buff = source.controller.card(buff, source=source)
        buff.tags[GameTag.DEATHRATTLE] = True
        buff.source = source
        return buff

    def do(self, source, target, buff):
        log.info("%r copy deathrattle from %r by %r", source, target, buff)
        if target.has_deathrattle:
            for deathrattle in target.deathrattles:
                source.additional_deathrattles.append(deathrattle)
            buff.apply(source)
            for entity in target.buffs:
                if not entity.has_deathrattle:
                    continue
                new_buff = self.create_buff(source)
                if hasattr(entity, "store_card"):
                    new_buff.store_card = entity.store_card
                for deathrattle in entity.deathrattles:
                    new_buff.additional_deathrattles.append(deathrattle)
                new_buff.apply(source)
        source.game.manager.targeted_action(self, source, target, buff)


class Counter(TargetedAction):
    """
    Counter a card, making it unplayable.
    """

    def do(self, source, target):
        target.cant_play = True
        source.game.manager.targeted_action(self, source, target)


class Predamage(TargetedAction):
    """
    Predamage target by \a amount.
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount):
        amount <<= target.incoming_damage_multiplier
        target.predamage = amount
        if amount:
            self.broadcast(source, EventListener.ON, target, amount)
            return source.game.trigger_actions(source, [Damage(target)])[0][0]
        return 0


class PutOnTop(TargetedAction):
    """
    Put card on deck top
    """

    TARGET = ActionArg()
    CARD = CardArg()

    def do(self, source, target, cards):
        log.info("%r put on %s's deck top", cards, target)
        if not isinstance(cards, list):
            cards = [cards]

        if cards:
            target.shuffle_deck()

        for card in cards:
            if card.controller != target:
                card.zone = Zone.SETASIDE
                card.controller = target
            if card.zone != Zone.DECK and len(target.deck) >= target.max_deck_size:
                log.info("Put(%r) fails because %r's deck is full", card, target)
                continue
            card.zone = Zone.DECK
            card, card.controller.deck[-1] = card.controller.deck[-1], card
            source.game.manager.targeted_action(self, source, target, card)


class Damage(TargetedAction):
    """
    Damage target by \a amount.
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount=None):
        if not amount:
            amount = target.predamage
        amount = target._hit(amount)
        target.predamage = 0
        if (
            source.type == CardType.MINION or source.type == CardType.HERO
        ) and source.stealthed:
            # TODO this should be an event listener of sorts
            source.stealthed = False
        source.game.manager.targeted_action(self, source, target, amount)
        if amount:
            # check hasattr: some sources of damage are game or player (like fatigue)
            # weapon damage itself after hero attack, but does not trigger lifesteal
            if (
                hasattr(source, "lifesteal")
                and source.lifesteal
                and source.type != CardType.WEAPON
            ):
                source.heal(source.controller.hero, amount)
            self.broadcast(source, EventListener.ON, target, amount, source)
            # poisonous can not destroy hero
            if (
                hasattr(source, "poisonous")
                and source.poisonous
                and (target.type != CardType.HERO and source.type != CardType.WEAPON)
            ):
                target.destroy()
            if (
                hasattr(source, "has_overkill")
                and source.has_overkill
                and source.controller.current_player
                and target.type != CardType.WEAPON
                and target.health < 0
            ):
                if source.type == CardType.HERO:
                    actions = source.controller.weapon.get_actions("overkill")
                else:
                    actions = source.get_actions("overkill")
                if actions:
                    source.game.trigger(source, actions, event_args=None)
            target.damaged_this_turn += amount
            if source.type == CardType.HERO_POWER:
                source.controller.hero_power_damage_this_game += amount
        return amount


class Deathrattle(TargetedAction):
    """
    Trigger deathrattles on card targets.
    """

    def do(self, source, target):
        if not target.has_deathrattle:
            return

        for entity in target.entities:
            source.game.manager.targeted_action(self, source, target)
            for deathrattle in entity.deathrattles:
                if callable(deathrattle):
                    actions = deathrattle(entity)
                else:
                    actions = deathrattle
                source.game.queue_actions(entity, actions)

                if target.controller.extra_deathrattles:
                    log.info("Triggering deathrattles for %r again", target)
                    source.game.queue_actions(entity, actions)


class Battlecry(TargetedAction):
    """
    Trigger Battlecry on card targets
    """

    CARD = CardArg()
    TARGET = ActionArg()

    def get_target_args(self, source, target):
        arg = self._args[1]
        if isinstance(arg, Selector):
            arg = arg.eval(source.game, source)
            assert len(arg) == 1
            arg = arg[0]
        elif isinstance(arg, LazyValue):
            arg = arg.evaluate(source)
            if hasattr(arg, "__iter__"):
                arg = arg[0]
        else:
            arg = _eval_card(source, arg)[0]
        return [arg]

    def has_extra_battlecries(self, player, card):
        # Brann Bronzebeard
        if player.extra_battlecries and card.has_battlecry:
            return True

        # Spirit of the Shark
        if card.type == CardType.MINION:
            if player.minion_extra_combos and card.has_combo and player.combo:
                return True
            if player.minion_extra_battlecries and card.has_battlecry:
                return True

        return False

    def do(self, source, card, target=None):
        player = source.controller

        if card.has_combo and player.combo:
            log.info("Activating %r combo targeting %r", card, target)
            actions = card.get_actions("combo")
        else:
            log.info("Activating %r action targeting %r", card, target)
            actions = card.get_actions("play")

        if card.battlecry_requires_target() and not target:
            log.info("%r requires a target for its battlecry. Will not trigger.")
            return

        source.game.manager.targeted_action(self, source, card, target)
        source.target = target
        source.game.main_power(source, actions, target)

        if self.has_extra_battlecries(player, card):
            source.game.main_power(source, actions, target)

        if card.overload:
            source.game.queue_actions(card, [Overload(player, card.overload)])


class ExtraBattlecry(Battlecry):
    def has_extra_battlecries(self, player, card):
        return False

    def do(self, source, card, target=None):
        if target is None:
            old_requirements = source.requirements
            source.requirements = card.requirements
            if source.requires_target():
                target = random.choice(source.play_targets)
            source.requirements = old_requirements

        return super().do(source, card, target)


class PlayHeroPower(TargetedAction):
    HERO_POWER = CardArg()
    TARGET = ActionArg()

    def do(self, source, heropower, targets):
        actions = heropower.get_actions("activate")
        if not hasattr(targets, "__iter__"):
            targets = [targets]
        for target in targets:
            heropower.target = target
            source.game.manager.targeted_action(self, source, heropower, target)
            source.game.main_power(heropower, actions, target)


class Destroy(TargetedAction):
    """
    Destroy character targets.
    """

    def do(self, source, target):
        if getattr(target, "dormant", False):
            log.info("%r is dormant cannot be destroyed", target)
            return
        if target.delayed_destruction:
            #  If the card is in PLAY, it is instead scheduled to be destroyed
            # It will be moved to the graveyard on the next Death event
            log.info("%r marks %r for imminent death", source, target)
            target.to_be_destroyed = True
            source.game.manager.targeted_action(self, source, target)
        else:
            log.info("%r destroys %r", source, target)
            if target.type == CardType.ENCHANTMENT:
                target.remove()
            else:
                target.zone = Zone.GRAVEYARD
                source.game.manager.targeted_action(self, source, target)


class Discard(TargetedAction):
    """
    Discard card targets in a player's hand
    """

    def do(self, source, target):
        self.broadcast(source, EventListener.ON, target)
        log.info("Discarding %r", target)
        old_zone = target.zone
        target.zone = Zone.GRAVEYARD
        source.game.manager.targeted_action(self, source, target)
        if old_zone == Zone.HAND:
            target.tags[DISCARDED] = True
            actions = target.get_actions("discard")
            source.game.cheat_action(target, actions)


class Discover(TargetedAction):
    """
    Opens a generic choice for three random cards matching a filter.
    """

    TARGET = ActionArg()
    CARDS = CardArg()
    CARD = CardArg()

    def get_target_args(self, source, target):
        if target.hero.data.card_class != CardClass.NEUTRAL:
            # use hero class for Discover if not neutral (eg. Ragnaros)
            discover_class = target.hero.data.card_class
        elif source.data.card_class != CardClass.NEUTRAL:
            # use card class for neutral hero classes
            discover_class = source.data.card_class
        else:
            # use random class for neutral hero classes with neutral cards
            discover_class = random_class()
        if "card_class" in self._args[1].filters:
            picker = self._args[1] * 3
            return [picker.evaluate(source)]
        picker = self._args[1] * 3
        picker = picker.copy_with_weighting(1, card_class=CardClass.NEUTRAL)
        picker = picker.copy_with_weighting(1, card_class=discover_class)
        return [picker.evaluate(source)]

    def do(self, source, target, cards):
        log.info("%r discovers %r for %s", source, cards, target)
        self.cards = cards
        player = source.controller
        player.choice = self
        self._callback = self.callback
        self.callback = []
        self.player = player
        self.source = source
        self.target = target
        self.cards = cards
        self.min_count = 1
        self.max_count = 1
        source.game.manager.targeted_action(self, source, target, cards)

    def choose(self, card):
        if card not in self.cards:
            raise InvalidAction(
                "%r is not a valid choice (one of %r)" % (card, self.cards)
            )
        self.player.choice = None
        for action in self._callback:
            self.source.game.trigger(
                self.source, [action], [self.target, self.cards, card]
            )
        self.callback = self._callback
        self.trigger_choice_callback()


class Draw(TargetedAction):
    """
    Make player targets draw a card from their deck.
    """

    TARGET = ActionArg()
    CARD = CardArg()

    def get_target_args(self, source, target):
        args = super().get_target_args(source, target)
        if args:
            card = args[0]
            if hasattr(card, "__iter__"):
                card = card[0]
            return [card]
        if target.deck:
            card = target.deck[-1]
        else:
            card = None
        return [card]

    def do(self, source, target, card):
        if card is None:
            target.fatigue()
            return []
        if len(target.hand) >= target.max_hand_size:
            log.info("%s overdraws and loses %r!", target, card)
            card.discard()
        else:
            log.info("%s draws %r", target, card)
            card.zone = Zone.HAND
            card.turn_drawn = source.game.turn
            source.controller.cards_drawn_this_turn += 1
            source.game.manager.targeted_action(self, source, target, card)
            if source.game.step > Step.BEGIN_MULLIGAN:
                # Proc the draw script, but only if we are past mulligan
                actions = card.get_actions("draw")
                source.game.cheat_action(card, actions)
            self.broadcast(source, EventListener.ON, target, card, source)

        return [card]


class Fatigue(TargetedAction):
    """
    Hit a player with a tick of fatigue
    """

    def do(self, source, target):
        if target.cant_fatigue:
            log.info("%s can't fatigue and does not take damage", target)
            return
        target.fatigue_counter += 1
        log.info("%s takes %i fatigue damage", target, target.fatigue_counter)
        source.game.manager.targeted_action(self, source, target)
        return source.game.queue_actions(
            source, [Hit(target.hero, target.fatigue_counter)]
        )


class ForceDraw(TargetedAction):
    """
    Draw card targets into their owners hand
    """

    def do(self, source, target):
        target.draw()
        return [target]


class DrawUntil(TargetedAction):
    """
    Make target player target draw up to \a amount cards minus their hand count.
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount):
        if target not in target.game.players:
            raise InvalidAction("%r is not a player" % target)
        difference = max(0, amount - len(target.hand))
        if difference > 0:
            return source.game.queue_actions(source, [Draw(target) * difference])


class FullHeal(TargetedAction):
    """
    Fully heal character targets.
    """

    def do(self, source, target):
        source.heal(target, target.max_health)


class GainArmor(TargetedAction):
    """
    Make hero targets gain \a amount armor.
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount):
        target.armor += amount
        source.game.manager.targeted_action(self, source, target, amount)
        self.broadcast(source, EventListener.ON, target, amount)


class GainMana(TargetedAction):
    """
    Give player targets \a Mana crystals.
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def get_target_args(self, source, target):
        ret = super().get_target_args(source, target)
        amount = ret[0]
        if target.max_mana + amount > target.max_resources:
            amount = target.max_resources - target.max_mana
        return [amount]

    def do(self, source, target, amount):
        target.max_mana = max(target.max_mana + amount, 0)
        source.game.manager.targeted_action(self, source, target, amount)


class SpendMana(TargetedAction):
    """
    Make player targets spend \a amount Mana.
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount):
        log.info("%s pays %i mana", target, amount)
        _amount = amount
        if target.temp_mana:
            # Coin, Innervate etc
            used_temp = min(target.temp_mana, amount)
            _amount -= used_temp
            target.temp_mana -= used_temp
        target.used_mana = max(target.used_mana + _amount, 0)
        source.game.manager.targeted_action(self, source, target, amount)
        self.broadcast(source, EventListener.AFTER, target, amount)


class SetMana(TargetedAction):
    """
    Set player to targets Mana crystals.
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount):
        old_mana = target.mana
        target.max_mana = amount
        target.used_mana = max(
            0, target.max_mana - target.overload_locked - old_mana + target.temp_mana
        )
        source.game.manager.targeted_action(self, source, target, amount)


class Give(TargetedAction):
    """
    Give player targets card \a id.
    """

    TARGET = ActionArg()
    CARD = CardArg()

    def do(self, source, target, cards):
        log.info("Giving %r to %s", cards, target)
        ret = []
        if not hasattr(cards, "__iter__"):
            # Support Give on multiple cards at once (eg. Echo of Medivh)
            cards = [cards]
        for card in cards:
            if len(target.hand) >= target.max_hand_size:
                log.info("Give(%r) fails because %r's hand is full", card, target)
                continue
            card.controller = target
            card.zone = Zone.HAND
            ret.append(card)
            source.game.manager.targeted_action(self, source, target, card)
            self.broadcast(source, EventListener.AFTER, target, card)
        return ret


class Hit(TargetedAction):
    """
    Hit character targets by \a amount.
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount):
        amount = source.get_damage(amount, target)
        if amount:
            source.game.manager.targeted_action(self, source, target, amount)
            return source.game.queue_actions(source, [Predamage(target, amount)])[0][0]
        return 0


class HitExcessDamage(TargetedAction):
    """
    Hit character targets by \a amount and excess damage to other.
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount):
        amount = source.get_damage(amount, target)
        if amount:
            source.game.manager.targeted_action(self, source, target, amount)
            if target.health >= amount:
                source.game.queue_actions(source, [Predamage(target, amount)])
                return 0
            else:
                excess_amount = amount - target.health
                source.game.queue_actions(source, [Predamage(target, amount)])
                return excess_amount
        return 0


class Heal(TargetedAction):
    """
    Heal character targets by \a amount.
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount):
        if source.controller.healing_as_damage:
            return source.game.queue_actions(source.controller, [Hit(target, amount)])

        amount = source.get_heal(amount, target)
        amount = min(amount, target.damage)
        if amount:
            # Undamaged targets do not receive heals
            log.info("%r heals %r for %i", source, target, amount)
            target.damage -= amount
            source.game.manager.targeted_action(self, source, target, amount)
            self.queue_broadcast(self, (source, EventListener.ON, target, amount))
            target.healed_this_turn += amount
            source.controller.healed_this_game += amount


class ManaThisTurn(TargetedAction):
    """
    Give player targets \a amount Mana this turn.
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount):
        target.temp_mana += min(target.max_resources - target.mana, amount)
        source.game.manager.targeted_action(self, source, target, amount)


class Mill(TargetedAction):
    """
    Mill \a count cards from the top of the player targets' deck.
    """

    TARGET = ActionArg()
    CARD = CardArg()

    def get_target_args(self, source, target):
        if target.deck:
            card = target.deck[-1]
        else:
            card = None
        return [card]

    def do(self, source, target, card):
        if card is None:
            return []
        source.game.manager.targeted_action(self, source, target, card)
        card.discard()
        self.broadcast(source, EventListener.ON, target, card, source)

        return [card]


class Morph(TargetedAction):
    """
    Morph minion target into \a minion id
    """

    TARGET = ActionArg()
    CARD = CardArg()

    def get_target_args(self, source, target):
        card = _eval_card(source, self._args[1])
        assert len(card) == 1
        card = card[0]
        card.controller = target.controller
        return [card]

    def do(self, source, target, card):
        log.info("Morphing %r into %r", target, card)
        target_zone = target.zone
        if card.zone != target_zone:
            # Transfer the zone position
            card._summon_index = target.zone_position
            # In-place morph is OK, eg. in the case of Lord Jaraxxus
            card.zone = target_zone
        target.clear_buffs()
        target.zone = Zone.SETASIDE
        target.morphed = card
        source.game.manager.targeted_action(self, source, target, card)
        return card


class FillMana(TargetedAction):
    """
    Refill \a amount mana crystals from player targets.
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount):
        target.used_mana = max(0, target.used_mana - amount)
        source.game.manager.targeted_action(self, source, target, amount)


class Retarget(TargetedAction):
    TARGET = ActionArg()
    CARD = CardArg()

    def do(self, source, target, new_target):
        if not new_target:
            return
        if isinstance(new_target, list):
            assert len(new_target) == 1
            new_target = new_target[0]
        if target.type in (CardType.HERO, CardType.MINION) and target.attacking:
            log.info("Retargeting %r's attack to %r", target, new_target)
            source.game.proposed_defender.defending = False
            source.game.proposed_defender = new_target
        else:
            log.info("Retargeting %r from %r to %r", target, target.target, new_target)
            target.target = new_target
        source.game.manager.targeted_action(self, source, target, new_target)

        return new_target


class Reveal(TargetedAction):
    """
    Reveal secret targets.
    """

    def do(self, source, target):
        log.info("Revealing %r", target)
        if target.zone == Zone.SECRET and target.data.secret:
            self.broadcast(source, EventListener.ON, target)
            target.zone = Zone.GRAVEYARD
        source.game.manager.targeted_action(self, source, target)


class SetCurrentHealth(TargetedAction):
    """
    Sets the current health of the character target to \a amount.
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount):
        log.info("Setting current health on %r to %i", target, amount)
        maxhp = target.max_health
        target.damage = max(0, maxhp - amount)
        source.game.manager.targeted_action(self, source, target, amount)
        return target


class SetTag(TargetedAction):
    """
    Sets targets' given tags.
    """

    TARGET = ActionArg()
    TAGS = ActionArg()

    def do(self, source, target, tags):
        if isinstance(tags, dict):
            for tag, value in tags.items():
                target.tags[tag] = value
        else:
            for tag in tags:
                target.tags[tag] = True
        self.broadcast(source, EventListener.AFTER, target)


class UnsetTag(TargetedAction):
    """
    Unset targets' given tags.
    """

    TARGET = ActionArg()
    TAGS = ActionArg()

    def do(self, source, target, tags):
        for tag in tags:
            target.tags[tag] = False


class SetAttribute(TargetedAction):
    TARGET = ActionArg()
    KEY = ActionArg()
    VALUE = ActionArg()

    def do(self, source, target, key, value):
        setattr(target, key, value)


class DelAttribute(TargetedAction):
    TARGET = ActionArg()
    KEY = ActionArg()

    def do(self, source, target, key):
        delattr(target, key)


class GetAttribute(TargetedAction):
    TARGET = ActionArg()
    KEY = ActionArg()

    def do(self, source, target, key):
        return getattr(target, key)


class Silence(TargetedAction):
    """
    Silence minion targets.
    """

    def do(self, source, target):
        log.info("Silencing %r", self)
        if target.type != CardType.MINION:
            return
        self.broadcast(source, EventListener.ON, target)
        target.clear_buffs()
        for attr in target.silenceable_attributes:
            if getattr(target, attr):
                setattr(target, attr, False)

        # Wipe the event listeners
        target._events = []
        target.silenced = True
        source.game.manager.targeted_action(self, source, target)


class Summon(TargetedAction):
    """
    Make player targets summon \a id onto their field.
    This works for equipping weapons as well as summoning minions.
    """

    TARGET = ActionArg()
    CARD = CardArg()

    def _broadcast(self, entity, source, at, *args):
        # Prevent cards from triggering off their own summon
        if entity is args[1]:
            return
        return super()._broadcast(entity, source, at, *args)

    def get_summon_index(self, source_index):
        return source_index + 1

    def do(self, source, target, cards):
        log.info("%s summons %r", target, cards)
        if not isinstance(cards, list):
            cards = [cards]

        for card in cards:
            if not card.is_summonable():
                continue
            if card.controller != target:
                card.controller = target
            # Poisoned Blade
            if (
                card.controller.weapon
                and card.controller.weapon.id == "AT_034"
                and source.type == CardType.HERO_POWER
                and card.type == CardType.WEAPON
            ):
                continue
            if card.zone != Zone.PLAY:
                if source.type == CardType.MINION:
                    if source.zone == Zone.PLAY:
                        source_index = source.controller.field.index(source)
                        card._summon_index = self.get_summon_index(source_index)
                    elif source.zone == Zone.GRAVEYARD:
                        card._summon_index = getattr(source, "_dead_position", None)
                        if card._summon_index is not None:
                            card._summon_index += cards.index(card)
                card.zone = Zone.PLAY
            if card.type == CardType.MINION and Race.TOTEM in card.races:
                card.controller.times_totem_summoned_this_game += 1
            source.game.manager.targeted_action(self, source, target, card)
            self.queue_broadcast(self, (source, EventListener.ON, target, card))
            self.broadcast(source, EventListener.AFTER, target, card)

        return cards


class SummonBothSides(Summon):
    TARGET = ActionArg()
    CARD = CardArg()

    def get_summon_index(self, source_index):
        return source_index + ((self.trigger_index + 1) % 2)


class SummonCustomMinion(TargetedAction):
    """
    Summon custom minion with cost/atk/max_health
    """

    TARGET = ActionArg()
    CARD = CardArg()
    COST = IntArg()
    ATK = IntArg()
    HEALTH = IntArg()

    def do(self, source, target, cards, cost, atk, health):
        if health <= 0:
            return
        if not isinstance(cards, list):
            cards = [cards]
        for card in cards:
            card.custom_card = True

            def create_custom_card(card):
                card.cost = cost
                card.atk = atk
                card.max_health = health

            card.create_custom_card = create_custom_card
            card.create_custom_card(card)

            if card.is_summonable():
                source.game.queue_actions(source, [Summon(target, card)])


class Shuffle(TargetedAction):
    """
    Shuffle card targets into player target's deck.
    """

    TARGET = ActionArg()
    CARD = CardArg()

    def do(self, source, target, cards):
        log.info("%r shuffles into %s's deck", cards, target)
        if not isinstance(cards, list):
            cards = [cards]

        for card in cards:
            if card.controller != target:
                card.zone = Zone.SETASIDE
                card.controller = target
            if len(target.deck) >= target.max_deck_size:
                log.info("Shuffle(%r) fails because %r's deck is full", card, target)
                continue
            card.zone = Zone.DECK
            target.shuffle_deck()
            source.game.manager.targeted_action(self, source, target, card)
            self.broadcast(source, EventListener.AFTER, target, card)


class Swap(TargetedAction):
    """
    Swap minion target with \a other.
    Behaviour is undefined when swapping more than two minions.
    """

    TARGET = ActionArg()
    OTHER = ActionArg()

    def get_target_args(self, source, target):
        other = self.eval(self._args[1], source)
        if not other:
            return (None,)
        assert len(other) == 1
        return [other[0]]

    def do(self, source, target, other):
        if other is not None:
            orig = target.zone
            target.zone = other.zone
            other.zone = orig
            source.game.manager.targeted_action(self, source, target, other)


class SwapController(TargetedAction):
    def do(self, source, card):
        old_zone = card.zone
        card.zone = Zone.SETASIDE
        card.controller = card.controller.opponent
        card.zone = old_zone
        source.game.manager.targeted_action(self, source, card)


class Steal(TargetedAction):
    """
    Make the controller take control of targets.
    The controller is the controller of the source of the action.
    """

    TARGET = ActionArg()
    CONTROLLER = ActionArg()

    def get_target_args(self, source, target):
        if len(self._args) > 1:
            # Controller was specified
            controller = self.eval(self._args[1], source)
            assert len(controller) == 1
            controller = controller[0]
        else:
            # Default to the source's controller
            controller = source.controller
        return [controller]

    def do(self, source, target, controller):
        log.info("%s takes control of %r", controller, target)
        zone = target.zone
        target.zone = Zone.SETASIDE
        target.controller = controller
        target.turns_in_play = 0  # To ensure summoning sickness
        target.zone = zone
        source.game.manager.targeted_action(self, source, target, controller)


class UnlockOverload(TargetedAction):
    """
    Unlock the target player's overload, both current and owed.
    """

    def do(self, source, target):
        log.info("%s overload gets cleared", target)
        target.overloaded = 0
        target.overload_locked = 0
        source.game.manager.targeted_action(self, source, target)


class SummonJadeGolem(TargetedAction):
    """
    Summons a Jade Golem for target player according to his Jade Golem Status
    """

    TARGET = ActionArg()
    CARD = CardArg()

    def get_target_args(self, source, target):
        jade_id = f"CFM_712_t{target.jade_golem:02d}"
        return _eval_card(source, jade_id)

    def do(self, source, target, card):
        log.info("%s summons a Jade Golem for %s", source, target)
        target.jade_golem = min(
            30, target.jade_golem + 1
        )  # Jade golem maximum of 30/30.
        if card.is_summonable():
            source.game.queue_actions(source, [Summon(target, card)])


class CastSpell(TargetedAction):
    """
    Cast a spell target random
    """

    SPELL = CardArg()
    SPELL_TARGET = CardArg()

    def get_target_args(self, source, target):
        ret = super().get_target_args(source, target)
        spell_target = [None]
        if ret:
            spell_target = ret[0]
        return [spell_target]

    def choose_target(self, source, card):
        return random.choice(card.targets)

    def do(self, source, card, targets):
        player = source.controller
        old_choice = player.choice
        player.choice = None

        if card.twinspell:
            source.game.queue_actions(card, [Give(player, card.twinspell_copy)])
        if card.must_choose_one:
            card = random.choice(card.choose_cards)
        for target in targets:
            if card.requires_target() and not target:
                if len(card.targets) > 0:
                    if target not in card.targets:
                        target = self.choose_target(source, card)
                else:
                    log.info("%s cast spell %s don't have a legal target", source, card)
                    return
            card.target = target
            card.zone = Zone.PLAY
            log.info("%s cast spell %s target %s", source, card, target)
            source.game.manager.targeted_action(self, source, card, target)
            source.game.queue_actions(card, [Battlecry(card, card.target)])
            while player.choice:
                choice = random.choice(player.choice.cards)
                log.info("Choosing card %r" % (choice))
                player.choice.choose(choice)
            while player.opponent.choice:
                choice = random.choice(player.opponent.choice.cards)
                log.info("Choosing card %r" % (choice))
                player.opponent.choice.choose(choice)
            player.choice = old_choice


class CastSpellTargetsEnemiesIfPossible(CastSpell):
    def choose_target(self, source, card):
        enemy_targets = []
        for entity in card.targets:
            if entity.controller == source.controller.opponent:
                enemy_targets.append(entity)
        if enemy_targets:
            return random.choice(enemy_targets)
        return random.choice(card.targets)


class Evolve(TargetedAction):
    """
    Transform your minions into random minions that cost (\a amount) more
    """

    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount):
        cost = target.cost + amount
        card_set = RandomMinion(cost=cost).find_cards(source)
        if card_set:
            card = random.choice(card_set)
            return source.game.queue_actions(source, [Morph(target, card)])[0]


class ExtraAttack(TargetedAction):
    """
    Get target an extra attack change
    """

    TARGET = ActionArg()

    def do(self, source, target):
        log.info("%s gets an extra attack change.", target)
        target.num_attacks -= 1
        source.game.manager.targeted_action(self, source, target)


class SwapStateBuff(TargetedAction):
    """
    Swap stats between two minions using \a buff.
    """

    TARGET = ActionArg()
    OTHER = ActionArg()
    BUFF = ActionArg()

    def do(self, source, target, other, buff):
        log.info("swap state %s and %s", target, other)
        if not target or not other:
            return
        other = other[0]
        buff1 = source.controller.card(buff, source=source)
        buff1.source = source
        buff1._xcost = other.cost
        if other.type == CardType.MINION:
            buff1._xatk = other.atk
            buff1._xhealth = other.health
        buff2 = source.controller.card(buff, source=source)
        buff2.source = source
        buff2._xcost = target.cost
        if target.type == CardType.MINION:
            buff2._xatk = target.atk
            buff2._xhealth = target.health
        buff1.apply(target)
        buff2.apply(other)
        source.game.manager.targeted_action(self, source, target, other, buff)


class CopyStateBuff(TargetedAction):
    """
    Copy target state, buff on self
    """

    TARGET = ActionArg()
    OTHER = ActionArg()
    BUFF = ActionArg()

    def do(self, source, target, buff):
        target = target
        buff = source.controller.card(buff, source=source)
        buff.source = source
        buff._xatk = target.atk
        buff._xhealth = target.health
        buff.apply(source)
        source.game.manager.targeted_action(self, source, target, buff)


class SetStateBuff(TargetedAction):
    """
    Set target state, buff on self
    """

    TARGET = ActionArg()
    OTHER = ActionArg()
    BUFF = ActionArg()

    def do(self, source, target, buff):
        target = target
        buff = source.controller.card(buff, source=source)
        buff.source = source
        buff._xatk = source.atk
        buff._xhealth = source.health
        buff.apply(target)
        source.game.manager.targeted_action(self, source, target, buff)


class RefreshHeroPower(TargetedAction):
    """
    Helper to Refresh Hero Power
    """

    HEROPOWER = ActionArg()

    def do(self, source, heropower):
        log.info("Refresh Hero Power %s.", heropower)
        if heropower.heropower_disabled:
            return
        if not heropower.exhausted:
            return
        heropower.additional_activations_this_turn += 1
        source.game.manager.targeted_action(self, source, heropower)


class MultipleChoice(TargetedAction):
    PLAYER = ActionArg()
    choose_times = 2

    def do(self, source, player):
        self.player = player
        self.source = source
        self.min_count = 1
        self.max_count = 1
        self.choosed_cards = []
        self.player.choice = self
        self._callback = self.callback
        self.callback = []
        getattr(self, "do_step1")()
        source.game.manager.targeted_action(self, source, player)

    def choose(self, card):
        if card not in self.cards:
            raise InvalidAction(
                "%r is not a valid choice (one of %r)" % (card, self.cards)
            )
        else:
            self.choosed_cards.append(card)
            lens = len(self.choosed_cards)
            if lens < self.choose_times:
                getattr(self, f"do_step{lens+1}")()
            else:
                self.player.choice = None
                self.done()
                self.callback = self._callback
                self.trigger_choice_callback()


class GameStart(GameAction):
    """
    Setup game
    """

    def do(self, source):
        log.info("Game start")
        source.game.manager.game_action(self, source)
        self.broadcast(source, EventListener.ON)


class Adapt(TargetedAction):
    """
    Adapt target
    """

    TARGET = ActionArg()
    CARDS = CardArg()
    CARD = CardArg()

    def get_target_args(self, source, target):
        choices = [
            "UNG_999t10",
            "UNG_999t2",
            "UNG_999t3",
            "UNG_999t4",
            "UNG_999t5",
            "UNG_999t6",
            "UNG_999t7",
            "UNG_999t8",
            "UNG_999t13",
            "UNG_999t14",
        ]
        cards = random.sample(choices, 3)
        cards = [source.controller.card(card, source=source) for card in cards]
        return [cards]

    def do(self, source, target, cards):
        log.info("%r adapts %r for %s", source, cards, target)
        self.cards = cards
        player = source.controller
        player.choice = self
        self.player = player
        self.source = source
        self.target = target
        self.cards = cards
        self.min_count = 1
        self.max_count = 1
        source.game.manager.targeted_action(self, source, target, cards)

    def choose(self, card):
        if card not in self.cards:
            raise InvalidAction(
                "%r is not a valid choice (one of %r)" % (card, self.cards)
            )
        self.player.choice = None
        self.source.game.trigger(self.source, (Battlecry(card, self.target),), None)
        self.trigger_choice_callback()


class AddProgress(TargetedAction):
    """
    Add Progress for target, such as quest card and upgradeable card
    """

    TARGET = ActionArg()
    CARD = CardArg()
    AMOUNT = IntArg()

    def do(self, source, target, card, amount=1):
        log.info("%r add progress from %r", target, card)
        if not target:
            return
        target.add_progress(card, amount)
        source.game.manager.targeted_action(self, source, target, card, amount)
        if target.progress >= target.progress_total:
            source.game.trigger(target, target.get_actions("reward"), event_args=None)
            if target.data.quest or target.data.sidequest:
                target.zone = Zone.GRAVEYARD


class ClearProgress(TargetedAction):
    """
    Clear Progress for target
    """

    def do(self, source, target):
        log.info("%r clear progress", target)
        target.clear_progress()
        source.game.manager.targeted_action(self, source, target)


class LosesDivineShield(TargetedAction):
    """
    Losses Divine Shield
    """

    def do(self, source, target):
        target.divine_shield = False
        source.game.manager.targeted_action(self, source, target)
        self.broadcast(source, EventListener.AFTER, target)


class Remove(TargetedAction):
    """
    Remove character targets
    """

    def do(self, source, target):
        target.zone = Zone.REMOVEDFROMGAME
        source.game.manager.targeted_action(self, source, target)


class Replay(TargetedAction):
    """
    Cast it if it's spell, otherwise summon it (minion, weapon, hero).
    Now only for Tess Greymane (GIL_598)
    """

    def do(self, source, target):
        source.game.manager.targeted_action(self, source, target)
        if target.type == CardType.SPELL:
            source.game.queue_actions(source, [CastSpell(target)])
        else:
            source.game.queue_actions(source, [Summon(source.controller, target)])


class Invoke(TargetedAction):
    def do(self, source, player):
        source.game.manager.targeted_action(self, source, player)
        player.invoke_counter += 1
        galakrond = player.galakrond
        if not galakrond:
            return
        source.game.queue_actions(
            source,
            [
                Reveal(galakrond),
                PlayHeroPower(galakrond.data.hero_power, None),
                AddProgress(galakrond, source),
            ],
        )


class Awaken(TargetedAction):
    def do(self, source, target):
        if not target.dormant:
            return
        target.dormant = False
        target.turns_in_play = 0
        source.game.manager.targeted_action(self, source, target)
        actions = target.get_actions("awaken")
        if actions:
            source.game.trigger(target, actions, event_args=None)


class Dormant(TargetedAction):
    TARGET = ActionArg()
    AMOUNT = IntArg()

    def do(self, source, target, amount):
        target.dormant = True
        target.dormant_turns += amount
        source.game.manager.targeted_action(self, source, target, amount)
