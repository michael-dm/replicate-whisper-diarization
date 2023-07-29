from predict import Predictor
import os

print("Arch: ", os.uname().machine)
dir_path = os.path.dirname(os.path.realpath(__file__))

predictor = Predictor()
print("Loading model...")
predictor.setup()

print("Predicting...")
result = predictor.speech_to_text(
    filepath=os.path.join(dir_path, 'test 2.mp3'),
    num_speakers=3,
    prompt='Une conversation entre plusieurs personnes.',
    group_segments=True
)
print("Result:")
print(result)