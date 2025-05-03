# ml_models.py
from unsloth import FastModel
# Global variables to store model and tokenizer
model = None
tokenizer = None
import torch
import gc
    

def initialize_model():
    torch.cuda.empty_cache()
    """Initialize the model and tokenizer once"""
    global model, tokenizer
    
    # Only load if not already loaded
    if model is None or tokenizer is None:
        print("Loading model and tokenizer...")
        torch._dynamo.config.disable = True
        model, tokenizer = FastModel.from_pretrained(
            model_name="MailMindActionItems5",
            max_seq_length=2048,
            load_in_4bit=True,
            device_map="cuda:0"
        )
        print("Model loaded successfully")
    
    return model, tokenizer


model, tokenizer = initialize_model()
def format_prompt(body):
    return (
        "<|system|>Provide the action item(s) for this email. Action items are defined as things the sender of the email expects the receiver to complete, such as questions and requests. You should try to keep the language the same (for example if it says Tuesday don't return Tues.). Many emails do not have action items, in this case, return No action. Huge mailing lists should never have action items. You need to keep in mind that emails being forwarded or replies to emails that had action items doesn't necessarily mean that the receiver needs to do what the original email says. You should use context to figure that out.<|user|>\n" + body + "\n<|assistant|>"
    )

def get_an_action(email):  
    global model, tokenizer
    
    gen_kwargs = {
        "max_new_tokens": 140,
        "do_sample": False,
        "temperature": 1,
        "top_p": 0.9,
    }

    prompt = format_prompt(email)
    inputs = tokenizer(prompt, return_tensors="pt").to("cuda")

    with torch.no_grad():
        outputs = model.generate(**inputs, **gen_kwargs)

    decoded = tokenizer.decode(outputs[0], skip_special_tokens=True)
    prediction = decoded.split("<|assistant|>")[-1].strip()
    return prediction

