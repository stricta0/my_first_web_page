import streamlit as st

st.title("Podaj swój adres email aby się zapisać")
st.write("Po kliknięciu zatwierd wyślemy do ciebie wiadomość email z informajcami")

email_adres = st.text_input("email:")
if st.button("Zatwierdź"):
    st.success(f"Wiadomość email została wyslana na adres, {email_adres}!")
