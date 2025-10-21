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
        model, tokenizer = FastModel.from_pretrained(
            model_name="MailMindSummarization",
            max_seq_length=2048,
            load_in_4bit=True,
            device_map="cuda:0"
        )
        print("Model loaded successfully")
    
    return model, tokenizer

# Load model at module import time
model, tokenizer = initialize_model()
counter = 0

def get_final_summary(summaries):
    try:
        # Format summaries into a more structured text
        summaries_text = ""
        for i, summary in enumerate(summaries):
            summaries_text += f"Email {i+1}: {summary.get('summary', 'No summary available')}\n"
            
        prompt = f"You've been given a list of summaries and action items from the day's emails. I need you to generate a nice summary and action items of the entire day. Please keep in mind the following: 1. You need to make sure your summary and action items puts important things up front. 2. Keep mailing list summaries to the end 3. Mark important things with IMPORTANT: Note that summaries clearly generated from companies like auto-send mailing lists should not be marked with important. It's ok if nothing is marked important. You should return a markdown formatted response. Keep in mind that only very important things should go up front, and mailing lists should not appear in both important and the summaries of them - you have to decide what goes where.  \n{summaries_text}"
        
        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": prompt}]
        }]
        
        text = tokenizer.apply_chat_template(messages, add_generation_prompt=True)
        inputs = tokenizer(text, return_tensors="pt").to("cuda")
        
        
        with torch.inference_mode():
            output_ids = model.generate(
                input_ids=inputs.input_ids,
                attention_mask=inputs.attention_mask,
                max_new_tokens=4096,
                temperature=1,
                top_p=0.95,
                top_k=64,
            )
        
        input_length = inputs.input_ids.shape[1]
        generated_ids = output_ids[0, input_length:]
        print(generated_ids)
        
        summary = tokenizer.decode(generated_ids, skip_special_tokens=True)
        print(summary)
        return summary
    except Exception as e:
        print(f"Error generating summary: {e}")
        return "Unable to generate summary due to an error."
