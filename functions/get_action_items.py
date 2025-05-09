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
            model_name="MailMindActionItems5",
            max_seq_length=2048,
            load_in_4bit=True,
            device_map="cuda:0"
        )
        print("Model loaded successfully")
    
    return model, tokenizer

# Load model at module import time
model, tokenizer = initialize_model()
counter = 0

def batch_get_action_items(emails, batch_size=8):  # Smaller default batch size
    global model, tokenizer

    all_actions = []
    processed_count = 0
    
    # Process in batches
    for i in range(0, len(emails), batch_size):
        # Force garbage collection and clear cache
        gc.collect()
        torch.cuda.empty_cache()
        
        curr_batch_size = min(batch_size, len(emails) - i)
        processed_count += curr_batch_size
        print(f"Processing batch {i//batch_size + 1}: emails {i+1}-{i+curr_batch_size} of {len(emails)}")
        
        batch_emails = emails[i:i+batch_size]
        
        try:
            # Create messages for each email in the batch
            batch_messages = [
                [{
                    "role": "user", 
                    "content": [{"type": "text", "text": f"Provide the action item(s) for this email. Action items are defined as things the sender of the email expects the reciever to complete, such as questions and requests. You should try to keep the language the same (for example if it says Tuesday don't return Tues.). Many emails do not have action items, in this case, return No action. Huge mailing lists should never have action items. You need to keep in mind that emails being forwarded or replies to emails that had action items doesn't necessarily mean that the reciever needs to do what the original email says. You should use context to figure that out.{email}"}]
                }]
                for email in batch_emails
            ]
            
            # Apply chat template to each message
            batch_texts = [
                tokenizer.apply_chat_template(
                    messages, 
                    add_generation_prompt=True
                )
                for messages in batch_messages
            ]
            
            # Process the batch
            batch_actions = []
            
            for j, text in enumerate(batch_texts):
                # Tokenize single input but keep in batch
                print(j)
                inputs = tokenizer(text, return_tensors="pt").to("cuda")
                
                # Generate output with memory optimizations
                with torch.inference_mode():
                    output_ids = model.generate(
                        input_ids=inputs.input_ids,
                        attention_mask=inputs.attention_mask,
                        max_new_tokens=256,
                        temperature=1.0,
                        top_p=0.95,
                        top_k=64,
                    )
                
                # Extract only the newly generated tokens
                input_length = inputs.input_ids.shape[1]
                generated_ids = output_ids[0, input_length:]
                
                # Decode only the generated part
                action_item = tokenizer.decode(generated_ids, skip_special_tokens=True)
                batch_actions.append(action_item)
                
                # Clean up individual tensors
                del inputs, output_ids, generated_ids
            
            # Add batch actions to overall results
            all_actions.extend(batch_actions)
            
            # Clean up batch variables
            del batch_messages, batch_texts, batch_actions
            torch.cuda.empty_cache()
            
        except RuntimeError as e:
            if "out of memory" in str(e):
                print(f"OOM error on batch {i//batch_size + 1}. Trying to recover...")
                
                # Emergency cleanup
                if 'inputs' in locals(): del inputs
                if 'output_ids' in locals(): del output_ids
                if 'batch_messages' in locals(): del batch_messages
                if 'batch_texts' in locals(): del batch_texts
                if 'batch_actions' in locals(): del batch_actions
                
                torch.cuda.empty_cache()
                gc.collect()
                
                # Process the problematic batch one by one as fallback
                print("Falling back to one-by-one processing for this batch")
                for email in batch_emails:
                    try:
                        # Process single email with minimal memory footprint
                        messages = [{
                            "role": "user", 
                            "content": [{"type": "text", "text": f"Provide the action item(s) for this email. Action items are defined as things the sender of the email expects the reciever to complete, such as questions and requests. You should try to keep the language the same (for example if it says Tuesday don't return Tues.). Many emails do not have action items, in this case, return No action. Huge mailing lists should never have action items. You need to keep in mind that emails being forwarded or replies to emails that had action items doesn't necessarily mean that the reciever needs to do what the original email says. You should use context to figure that out. {email}"}]
                        }]
                        
                        text = tokenizer.apply_chat_template(messages, add_generation_prompt=True)
                        inputs = tokenizer(text, return_tensors="pt").to("cuda")
                        
                        with torch.inference_mode():
                            output_ids = model.generate(
                                input_ids=inputs.input_ids,
                                attention_mask=inputs.attention_mask,
                                max_new_tokens=256,
                                temperature=0.7,  # Lower temperature for emergency mode
                                top_p=0.95,
                                top_k=64,
                            )
                        
                        input_length = inputs.input_ids.shape[1]
                        generated_ids = output_ids[0, input_length:]
                        action  = tokenizer.decode(generated_ids, skip_special_tokens=True)
                        all_actions.append(action)
                        
                        del inputs, output_ids, text, messages, generated_ids
                        torch.cuda.empty_cache()
                        gc.collect()
                        
                    except RuntimeError:
                        # If still OOM, add placeholder
                        all_actions.append("ERROR: Could not get action items due to memory limitations")
                        torch.cuda.empty_cache()
                        gc.collect()
            else:
                raise e
    
    return all_actions