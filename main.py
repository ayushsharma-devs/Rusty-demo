from voice_engine import listen, speak
from memory import store_fact, query_fact, list_memory

def run_showcase():
    speak("Hi! I'm Rusty. Ready when you are.")

    while True:
        user_input = listen()
        if not user_input:
            speak("Didn't catch that. Try again.")
            continue

        lowered = user_input.lower()

        if lowered.startswith("remember that"):
            try:
                key, value = lowered.split("remember that", 1)[1].strip().split(" is ")
                speak(store_fact(key.strip(), value.strip()))
            except ValueError:
                speak("Try saying it like: remember that your name is Rusty.")
        elif lowered.startswith("what is") or "do you remember" in lowered:
            query = lowered.replace("what is", "").replace("do you remember", "").strip()
            response = query_fact(query)
            speak(response)
        elif lowered in ["list memory", "show memory"]:
            speak(list_memory())
        elif lowered in ["exit", "quit", "stop"]:
            speak("Goodbye!")
            break
        else:
            speak("I don't understand that yet.")

if __name__ == "__main__":
    run_showcase()
