Multiple Inboxes

General Idea:
Create a Master model with an id, username, password, subscribed (moved from User), last_login, stripe id, and a many to one relationship with User
Switch our main "user" model from User to Master
Then we will have to do a lot of for user in Master.users in various places
This gives us the ability to have many emails associated with one account

Inital sign in :
We have them create a profile with just a username and password

Then we have a new page called portal where the following happens:
They can either add a Google or Outlook account
The way we do this is we create a new User and immediatley associate it with the Master
On the user's end, it looks like they sign in with google / outlook and then get brought back to portal where their email that they just added is now in a list with any others they may have added

Once we get this working can talk other steps like seperate tabs, how to handle it with the mailing lists, etc. 
Have to get MVP working with this idea first


Known problems I haven't thought through fully:
Calendar
High priority
Reply / Send
Stripe Email