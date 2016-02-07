import random
from itertools import chain
from hearthstone.enums import CardType, PlayState, Zone
from .actions import Concede, Draw, Fatigue, Give, Steal, Summon
from .aura import TargetableByAuras
from .card import Card
from .deck import Deck
from .entity import Entity
from .entity import slot_property
from .managers import PlayerManager
from .utils import CardList


class Player(Entity, TargetableByAuras):
	Manager = PlayerManager
	cant_overload = slot_property("cant_overload")
	extra_battlecries = slot_property("extra_battlecries")
	extra_deathrattles = slot_property("extra_deathrattles")
	healing_double = slot_property("healing_double", sum)
	hero_power_double = slot_property("hero_power_double", sum)
	outgoing_healing_adjustment = slot_property("outgoing_healing_adjustment")
	shadowform = slot_property("shadowform")
	spellpower_double = slot_property("spellpower_double", sum)
	spellpower_adjustment = slot_property("spellpower", sum)
	type = CardType.PLAYER

	def __init__(self, name, deck, hero):
		self.starting_deck = deck
		self.starting_hero = hero
		self.data = None
		self.name = name
		self.hero = None
		super().__init__()
		self.deck = Deck()
		self.hand = CardList()
		self.field = CardList()
		self.graveyard = CardList()
		self.secrets = CardList()
		self.choice = None
		self.max_hand_size = 10
		self.max_resources = 10
		self.cant_draw = False
		self.cant_fatigue = False
		self.fatigue_counter = 0
		self.last_card_played = None
		self.cards_drawn_this_turn = 0
		self.overloaded = 0
		self.overload_locked = 0
		self._max_mana = 0
		self._start_hand_size = 3
		self.playstate = PlayState.INVALID
		self.temp_mana = 0
		self.timeout = 75
		self.times_hero_power_used_this_game = 0
		self.used_mana = 0
		self.minions_killed_this_turn = 0
		self.weapon = None
		self.zone = Zone.INVALID

	def __str__(self):
		return self.name

	def __repr__(self):
		return "%s(name=%r, hero=%r)" % (self.__class__.__name__, self.name, self.hero)

	@property
	def current_player(self):
		return self.game.current_player is self

	@property
	def controller(self):
		return self

	@property
	def mana(self):
		mana = max(0, self.max_mana - self.used_mana - self.overload_locked) + self.temp_mana
		return mana

	@property
	def heropower_damage(self):
		return sum(minion.heropower_damage for minion in self.field)

	@property
	def spellpower(self):
		aura_power = self.controller.spellpower_adjustment
		minion_power = sum(minion.spellpower for minion in self.field)
		return aura_power + minion_power

	@property
	def start_hand_size(self):
		if not self.first_player:
			# Give the second player an extra card
			return self._start_hand_size + 1
		return self._start_hand_size

	@property
	def characters(self):
		return CardList(chain([self.hero] if self.hero else [], self.field))

	@property
	def entities(self):
		for entity in self.field:
			for e in entity.entities:
				yield e
		for secret in self.secrets:
			yield secret
		for buff in self.buffs:
			yield buff
		if self.hero:
			for entity in self.hero.entities:
				yield entity
		yield self

	@property
	def live_entities(self):
		for minion in self.field:
			yield minion
		if self.hero:
			yield self.hero
		if self.weapon:
			yield self.weapon

	@property
	def actionable_entities(self):
		for character in self.characters:
			yield character
		for card in self.hand:
			yield card
		if self.hero.power:
			yield self.hero.power

	@property
	def minion_slots(self):
		return max(0, self.game.MAX_MINIONS_ON_FIELD - len(self.field))

	def card(self, id, source=None, zone=Zone.SETASIDE):
		card = Card(id)
		card.controller = self
		card.zone = zone
		card.play_counter = self.game.play_counter
		self.game.play_counter += 1
		if source is not None:
			card.creator = source
		self.game.manager.new_entity(card)
		return card

	def prepare_for_game(self):
		self.summon(self.starting_hero)
		for id in self.starting_deck:
			self.card(id, zone=Zone.DECK)
		self.shuffle_deck()
		self.playstate = PlayState.PLAYING

		# Draw initial hand (but not any more than what we have in the deck)
		hand_size = min(len(self.deck), self.start_hand_size)
		starting_hand = random.sample(self.deck, hand_size)
		# It's faster to move cards directly to the hand instead of drawing
		for card in starting_hand:
			card.zone = Zone.HAND

	def concede(self):
		return self.game.queue_actions(self, [Concede(self)])

	def get_spell_damage(self, amount: int) -> int:
		"""
		Returns the amount of damage \a amount will do, taking
		SPELLPOWER and SPELLPOWER_DOUBLE into account.
		"""
		amount += self.spellpower
		amount <<= self.controller.spellpower_double
		return amount

	def give(self, id):
		cards = self.game.queue_actions(self, [Give(self, id)])[0]
		return cards[0][0]

	def discard_hand(self):
		self.log("%r discards their entire hand!", self)
		# iterate the list in reverse so we don't skip over cards in the process
		# yes it's stupid.
		for card in self.hand[::-1]:
			card.discard()

	def draw(self, count=1):
		if self.cant_draw:
			self.log("%s tries to draw %i cards, but can't draw", self, count)
			return None

		ret = self.game.queue_actions(self, [Draw(self) * count])[0]
		if count == 1:
			if not ret[0]:  # fatigue
				return None
			return ret[0][0]
		return ret

	def mill(self, count=1):
		if count == 1:
			if not self.deck:
				return
			else:
				card = self.deck[-1]
			self.log("%s mills %r", self, card)
			card.discard()
			return card
		else:
			ret = []
			while count:
				ret.append(self.mill())
				count -= 1
			return ret

	def fatigue(self):
		return self.game.queue_actions(self, [Fatigue(self)])[0]

	def pay_mana(self, amount: int) -> int:
		"""
		Make player pay \a amount mana.
		Returns how much mana is spent, after temporary mana adjustments.
		"""
		if self.temp_mana:
			# Coin, Innervate etc
			used_temp = min(self.temp_mana, amount)
			amount -= used_temp
			self.temp_mana -= used_temp
		self.log("%s pays %i mana", self, amount)
		self.used_mana += amount
		return amount

	@property
	def max_mana(self):
		return self._max_mana

	@max_mana.setter
	def max_mana(self, amount):
		self._max_mana = min(self.max_resources, max(0, amount))
		self.log("%s is now at %i mana crystals", self, self._max_mana)

	def steal(self, card):
		return self.game.queue_actions(self, [Steal(card)])

	def shuffle_deck(self):
		self.log("%r shuffles their deck", self)
		random.shuffle(self.deck)

	def summon(self, card):
		"""
		Puts \a card in the PLAY zone
		"""
		if isinstance(card, str):
			card = self.card(card, zone=Zone.PLAY)
		self.game.queue_actions(self, [Summon(self, card)])
		return card
