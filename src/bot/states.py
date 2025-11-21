from aiogram.fsm.state import StatesGroup, State

class TopupStates(StatesGroup):
    choosing_amount = State()
    choosing_method = State()
    receipt_choice = State()
    waiting_email = State()

class GenStates(StatesGroup):
    uploading_images = State()
    waiting_prompt = State()
    generating = State()
    final_menu = State()
    selecting_aspect_ratio = State()

class CreateStates(StatesGroup):
    waiting_prompt = State()
    generating = State()
    selecting_aspect_ratio = State()
    
class BroadcastStates(StatesGroup):
    waiting_for_message = State()
class VoiceStates(StatesGroup):
    confirming_prompt = State()    
    
    