        document.addEventListener('DOMContentLoaded', function() {
            const timeSelect = document.getElementById('time-select');
            const selectionCount = document.getElementById('selection-count');
            const maxSelections = 3;
            
            function updateSelectionCount() {
                const selectedCount = timeSelect.selectedOptions.length;
                selectionCount.textContent = `${selectedCount} of ${maxSelections} times selected`;
                
                if (selectedCount >= maxSelections) {
                    selectionCount.classList.add('max-reached');
                } else {
                    selectionCount.classList.remove('max-reached');
                }
            }
            
            timeSelect.addEventListener('change', function() {
                const selectedOptions = Array.from(timeSelect.selectedOptions);
                
                if (selectedOptions.length > maxSelections) {
                    // Remove the last selected option if limit exceeded
                    const lastSelected = selectedOptions[selectedOptions.length - 1];
                    lastSelected.selected = false;
                    
                    // Show warning message
                    selectionCount.textContent = `Maximum ${maxSelections} times allowed`;
                    selectionCount.classList.add('warning');
                    
                    setTimeout(() => {
                        updateSelectionCount();
                        selectionCount.classList.remove('warning');
                    }, 2000);
                } else {
                    updateSelectionCount();
                }
            });
            
            // Initial update
            updateSelectionCount();
        });